from liteflow import ChatCompletion, Input, Python, Workflow


def finish(text: str) -> str:
    return text.strip()


workflow = Workflow(
    name="reply",
    nodes=[ChatCompletion(model="support-model", messages=Input.messages), Python(fn=finish)],
)
