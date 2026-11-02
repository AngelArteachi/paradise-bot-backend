"""A stand-in for the OpenAI client that replays scripted responses and records every request."""

import json
from types import SimpleNamespace


def text(content):
    return SimpleNamespace(content=content, tool_calls=None)


def tool_call(name, arguments, call_id='call_1'):
    return SimpleNamespace(
        content=None,
        tool_calls=[SimpleNamespace(id=call_id, type='function', function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))],
    )


class FakeOpenAI:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        if not self.responses:
            raise AssertionError('The fake OpenAI client ran out of scripted responses')
        return SimpleNamespace(choices=[SimpleNamespace(message=self.responses.pop(0))])
