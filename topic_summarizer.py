# Modified from https://gist.github.com/rht/c5fd97d9171a5e71863d85e47af7a7e3
import os
import time
from typing import Any, Dict

from langchain import OpenAI, PromptTemplate
from langchain.docstore.document import Document
from langchain.chains.llm import LLMChain
from langchain.chains.summarize import load_summarize_chain
from langchain.chains.combine_documents.map_rerank import MapRerankDocumentsChain
from langchain.output_parsers.regex import RegexParser

import zulip

os.environ["OPENAI_API_KEY"] = "openai_api_key"
# Set the temperature to 0 for a more deterministic output
llm = OpenAI(temperature=0)


def create_topic_generator():
    # Creates a topic phrase for the conversation.
    topic_generator_prompt_template = """
    Write a topic phrase of at most 4 words of the following:

    {text}

    TOPIC PHRASE OF THE TEXT:"""
    TOPIC_GENERATOR_PROMPT = PromptTemplate(
        template=topic_generator_prompt_template, input_variables=["text"]
    )
    return load_summarize_chain(llm, chain_type="stuff", prompt=TOPIC_GENERATOR_PROMPT)


def create_stuff_summarizer():
    # Creates a summary of the conversation using the
    # stuff summarizer.
    return load_summarize_chain(llm, chain_type="stuff")


def create_refine_summarizer():
    refine_template = (
        "Your job is to produce a final summary of at most 4 words\n"
        "We have provided an existing summary up to a certain point: {existing_answer}\n"
        "We have the opportunity to refine the existing summary"
        "(only if needed) with some more context below.\n"
        "------------\n"
        "{text}\n"
        "------------\n"
        "Given the new context, refine the original summary"
        "If the context isn't useful, return the original summary."
    )
    refine_prompt = PromptTemplate(
        input_variables=["existing_answer", "text"],
        template=refine_template,
    )
    return load_summarize_chain(llm, chain_type="refine", refine_prompt=refine_prompt)


def create_map_reduce_summarizer():
    return load_summarize_chain(llm, chain_type="map_reduce")


def create_map_rerank_summarizer():
    map_template_string = """Given a part of a conversation below, your job is to produce a summary of it with at most 4 words
    and give a score from 0 to 100 on how confident you are with the answer in the following format:
    SUMMARY:
    SCORE:
    {text}
    """

    output_parser = RegexParser(
        regex=r"SUMMARY: (.+)\nSCORE: (\d+)", output_keys=["topic", "score"]
    )

    MAP_PROMPT = PromptTemplate(
        input_variables=["text"],
        template=map_template_string,
        output_parser=output_parser,
    )

    map_rerank = MapRerankDocumentsChain(
        llm_chain=LLMChain(llm=llm, prompt=MAP_PROMPT),
        rank_key="score",
        answer_key="topic",
        document_variable_name="text",
    )
    return map_rerank


chain_topic_generator = create_topic_generator()

chain_topic_summarizer_stuff = create_stuff_summarizer()
chain_topic_summarizer_refine = create_refine_summarizer()
chain_topic_summarizer_map_reduce = create_map_reduce_summarizer()
chain_topic_summarizer_map_rerank = create_map_rerank_summarizer()


def topic_from_string(text):
    return chain_topic_generator.run([Document(page_content=text)]).strip()


def exit_immediately(s):
    print("\nERROR\n", s)
    exit(1)


# Retrieves all messages matching request from Zulip, starting at post id anchor.
# As recommended in the Zulip API docs, requests 1000 messages at a time.
# Returns a list of messages.
def request_all(client, request, anchor=0):
    request["anchor"] = anchor
    request["num_before"] = 0
    request["num_after"] = 1000
    response = safe_request(client.get_messages, request)
    msgs = response["messages"]
    while not response["found_newest"]:
        request["anchor"] = response["messages"][-1]["id"] + 1
        response = safe_request(client.get_messages, request)
        msgs = msgs + response["messages"]
    return msgs


# runs client.cmd(args). If the response is a rate limit error, waits
# the requested time and then retries the request.
def safe_request(cmd, *args, **kwargs):
    rsp = cmd(*args, **kwargs)
    while rsp["result"] == "error":
        if "retry-after" in rsp:
            print("timeout hit: {}".format(rsp["retry-after"]))
            time.sleep(float(rsp["retry-after"]) + 1)
            rsp = cmd(*args, **kwargs)
        else:
            exit_immediately(rsp["msg"])
    return rsp


zulip_client = zulip.Client(config_file="./zuliprc")


def get_answer(message):
    stream_topic = message.split("#**")[1][:-2]
    stream_name, topic_name = stream_topic.split(">")
    request = {
        "narrow": [
            {"operator": "stream", "operand": stream_name},
            {"operator": "topic", "operand": topic_name},
        ],
        "client_gravatar": True,
        "apply_markdown": False,
    }

    enabled = {"refine", "stuff_summary", "map_rerank", "map_reduce_summary"}

    thread_content = request_all(zulip_client, request)
    thread_formatted = []
    for msg in thread_content:
        thread_formatted.append(f"{msg['sender_full_name']} said: {msg['content']}")

    print("Conversation text input from Zulip", thread_formatted)
    # texts = text_splitter.split_text(thread_txt)
    docs = [Document(page_content=t) for t in thread_formatted]
    print(f"Prepared documents: {docs}")

    output = ""
    if "refine" in enabled:
        # Generate a topic using the refine summarizer
        output += f"topic (refine): {chain_topic_summarizer_refine.run(docs).strip()}\n"
    if "stuff" in enabled:
        # Generate a topic using the stuff summarizer
        try:
            output += f"topic (stuff): {chain_topic_generator.run(docs).strip()}\n"
        except Exception as e:
            print(e)
            output += "topic (stuff): MAX_TOKEN_EXCEEDED\n"
    if "map_rerank" in enabled:
        # Generate a topic using maprerank summarizer
        try:
            output += (
                f"topic (map_rerank): {chain_topic_summarizer_map_rerank.run(docs)}\n"
            )
        except Exception as e:
            print(e)
            output += "topic (map_rerank): MAX_TOKEN_EXCEEDED\n"

    if "stuff_summary" in enabled:
        # Generate a summary using stuff summarizer, then generate a topic
        try:
            summary = chain_topic_summarizer_stuff.run(docs)
        except Exception as e:
            print(e)
            summary = "MAX_TOKEN_EXCEEDED"
        output += f"topic (stuff summary): {topic_from_string(summary)}\n"
        output += f"\n{summary}\n"
    if "map_reduce_summary" in enabled:
        # Generate a summary using mapreduce summarizer, then generate a topic
        try:
            summary = chain_topic_summarizer_map_reduce.run(docs)
        except Exception as e:
            print(e)
            summary = "MAX_TOKEN_EXCEEDED"
        output += f"topic (map_reduce summary): {topic_from_string(summary)}\n"
        output += f"\n{summary}\n"

    return output


# The code after this line could be simplified by https://github.com/zulip/python-zulip-api/pull/786
def handle_message(msg: Dict[str, Any]) -> None:
    print("processing", msg)
    if msg["type"] != "stream":
        return

    message = msg["content"]
    content = get_answer(message)
    request = {
        "type": "stream",
        "to": msg["display_recipient"],
        "topic": msg["subject"],
        "content": content,
    }
    print("sending", content)
    zulip_client.send_message(request)


def watch_messages() -> None:
    print("Watching for messages...")

    def handle_event(event: Dict[str, Any]) -> None:
        if "message" not in event:
            # ignore heartbeat events
            return
        handle_message(event["message"])

    # https://zulip.com/api/real-time-events
    narrow = [["is", "mentioned"]]
    zulip_client.call_on_each_event(
        handle_event, event_types=["message"], all_public_streams=True, narrow=narrow
    )


# If you want to test directly:
# get_answer("@**bot** #**integrations>docs question answer llm bot**")
watch_messages()
