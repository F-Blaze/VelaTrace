from unittest.mock import Mock

import pytest

from velatrace.errors import CapabilityError
from velatrace.tokens import LocalChatTokenizer, Prompt


def test_transformers_mapping_default_cannot_be_counted_as_two_tokens():
    counter = LocalChatTokenizer.__new__(LocalChatTokenizer)
    ids = list(range(112))
    counter.tokenizer = Mock()
    counter.tokenizer.apply_chat_template.side_effect = lambda *a, **kw: (
        ids if kw.get("return_dict") is False else {"input_ids": ids, "attention_mask": [1] * 112})
    prompt = Prompt("Return JSON.", "Synthetic test.")
    assert counter.count(prompt) == 112
    counter.tokenizer.apply_chat_template.assert_called_once_with(
        prompt.messages(), tokenize=True, add_generation_prompt=True, return_dict=False)


@pytest.mark.parametrize("result", [{"input_ids": [1, 2]}, [[1, 2]], [True], ["1"]])
def test_invalid_tokenizer_output_refuses_precise_estimate(result):
    counter = LocalChatTokenizer.__new__(LocalChatTokenizer)
    counter.tokenizer = Mock()
    counter.tokenizer.apply_chat_template.return_value = result
    with pytest.raises(CapabilityError, match="flat list"):
        counter.count(Prompt("system", "user"))
