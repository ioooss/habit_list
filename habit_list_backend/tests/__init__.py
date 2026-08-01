# tests 包：pytest 自动从 conftest.py 加载夹具（app_no_scheduler, client, test_settings 等）
# 这里只 re-export DashScope mock helper 函数，让测试用例可以 from tests import mock_xxx
from .fixtures import (  # noqa: F401
    mock_dashscope_chat_stream,
    mock_dashscope_embeddings,
    mock_dashscope_asr,
    mock_dashscope_tts,
)
