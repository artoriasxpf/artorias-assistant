"""Preserve DeepSeek thinking messages across Chat Completions tool calls."""
from langchain_core.messages import AIMessage
from langchain_deepseek import ChatDeepSeek


class DeepSeekChatModel(ChatDeepSeek):
    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        messages = self._convert_input(input_).to_messages()
        payload = super()._get_request_payload(messages, stop=stop, **kwargs)

        # Chat Completions serializes messages in order but omits this
        # DeepSeek-specific response field from the outgoing assistant message.
        for message, entry in zip(messages, payload.get('messages', [])):
            if isinstance(message, AIMessage) and entry['role'] == 'assistant':
                reasoning = message.additional_kwargs.get('reasoning_content')
                if isinstance(reasoning, str):
                    entry['reasoning_content'] = reasoning

        return payload
