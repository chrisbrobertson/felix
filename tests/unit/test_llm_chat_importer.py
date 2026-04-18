import json
import os
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

# Module under test
import llm_chat_importer


@pytest.fixture
def mock_memories_dir(tmp_path):
    """Redirect MEMORIES_DIR to a temp directory."""
    with patch.object(llm_chat_importer, "MEMORIES_DIR", tmp_path):
        yield tmp_path


def test_parse_chatgpt_format():
    """Test parsing ChatGPT conversations.json format."""
    data = [
        {
            "title": "Test Conversation",
            "create_time": 1609459200.0,  # 2021-01-01 00:00:00 UTC
            "mapping": {
                "node1": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["Hello, how are you?"]},
                        "create_time": 1609459200.0,
                    }
                },
                "node2": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["I'm doing well, thank you!"]},
                        "create_time": 1609459210.0,
                    }
                },
            }
        }
    ]

    conversations = llm_chat_importer._parse_chatgpt(data)

    assert len(conversations) == 1
    conv = conversations[0]
    assert conv["title"] == "Test Conversation"
    assert conv["platform"] == "chatgpt"
    assert len(conv["messages"]) == 2
    assert conv["messages"][0] == ("user", "Hello, how are you?")
    assert conv["messages"][1] == ("assistant", "I'm doing well, thank you!")


def test_parse_claude_format():
    """Test parsing Claude export JSON format."""
    data = [
        {
            "uuid": "test-uuid-123",
            "name": "Test Claude Chat",
            "created_at": "2021-01-01T00:00:00Z",
            "messages": [
                {
                    "uuid": "msg1",
                    "sender": "human",
                    "text": "What is AI?",
                    "created_at": "2021-01-01T00:00:00Z",
                },
                {
                    "uuid": "msg2",
                    "sender": "assistant",
                    "text": "AI stands for Artificial Intelligence.",
                    "created_at": "2021-01-01T00:00:10Z",
                },
            ]
        }
    ]

    conversations = llm_chat_importer._parse_claude(data)

    assert len(conversations) == 1
    conv = conversations[0]
    assert conv["title"] == "Test Claude Chat"
    assert conv["platform"] == "claude"
    assert conv["id"] == "test-uuid-123"
    assert len(conv["messages"]) == 2
    assert conv["messages"][0] == ("user", "What is AI?")
    assert conv["messages"][1] == ("assistant", "AI stands for Artificial Intelligence.")


def test_import_json_file_chatgpt(mock_memories_dir):
    """Test import_file with JSON bytes (ChatGPT format)."""
    data = [
        {
            "title": "Simple Chat",
            "create_time": 1609459200.0,
            "mapping": {
                "node1": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["Hello"]},
                        "create_time": 1609459200.0,
                    }
                },
                "node2": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["Hi there!"]},
                        "create_time": 1609459210.0,
                    }
                },
            }
        }
    ]

    json_bytes = json.dumps(data).encode()
    written = llm_chat_importer.import_file(json_bytes, "conversations.json", mock_memories_dir)

    assert len(written) == 1
    assert written[0].startswith("llm-chat-chatgpt-2021-01-01-simple-chat-")

    # Verify file was written
    files = list(mock_memories_dir.glob("*.md"))
    assert len(files) == 1
    assert files[0].name == written[0]


def test_import_zip_file_chatgpt(mock_memories_dir):
    """Test import_file with ZIP bytes containing conversations.json."""
    data = [
        {
            "title": "Zip Chat",
            "create_time": 1609459200.0,
            "mapping": {
                "node1": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["Test message"]},
                        "create_time": 1609459200.0,
                    }
                },
                "node2": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["Test response"]},
                        "create_time": 1609459210.0,
                    }
                },
            }
        }
    ]

    # Create ZIP file in memory
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w') as zf:
        zf.writestr("conversations.json", json.dumps(data))
    zip_bytes = zip_buffer.getvalue()

    written = llm_chat_importer.import_file(zip_bytes, "export.zip", mock_memories_dir)

    assert len(written) == 1
    assert written[0].startswith("llm-chat-chatgpt-2021-01-01-zip-chat-")


def test_memory_file_frontmatter(mock_memories_dir):
    """Test that written file has correct type, platform, and source_title."""
    data = [
        {
            "uuid": "test-123",
            "name": "Frontmatter Test",
            "created_at": "2021-01-01T00:00:00Z",
            "messages": [
                {
                    "sender": "human",
                    "text": "Hello",
                    "created_at": "2021-01-01T00:00:00Z",
                },
                {
                    "sender": "assistant",
                    "text": "Hello! How can I help you today?",
                    "created_at": "2021-01-01T00:00:10Z",
                },
            ]
        }
    ]

    json_bytes = json.dumps(data).encode()
    written = llm_chat_importer.import_file(json_bytes, "claude.json", mock_memories_dir)

    # Read the written file
    file_path = mock_memories_dir / written[0]
    content = file_path.read_text()

    # Check frontmatter
    assert "type: llm_chat" in content
    assert "platform: claude" in content
    assert "source_title: Frontmatter Test" in content
    assert "message_count: 2" in content
    assert "summary:" in content
    assert "id: test-123" in content


def test_empty_conversations_skipped(mock_memories_dir):
    """Test that conversations with no messages produce no files."""
    # ChatGPT format with no messages
    chatgpt_data = [
        {
            "title": "Empty Chat",
            "create_time": 1609459200.0,
            "mapping": {}
        }
    ]

    written = llm_chat_importer.import_file(
        json.dumps(chatgpt_data).encode(),
        "empty.json",
        mock_memories_dir
    )

    assert len(written) == 0

    # Claude format with no messages
    claude_data = [
        {
            "uuid": "empty-123",
            "name": "Empty Claude Chat",
            "created_at": "2021-01-01T00:00:00Z",
            "messages": []
        }
    ]

    written = llm_chat_importer.import_file(
        json.dumps(claude_data).encode(),
        "empty_claude.json",
        mock_memories_dir
    )

    assert len(written) == 0


def test_chatgpt_multiple_conversations(mock_memories_dir):
    """Test importing multiple conversations at once."""
    data = [
        {
            "title": "Chat One",
            "create_time": 1609459200.0,
            "mapping": {
                "node1": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["First chat"]},
                        "create_time": 1609459200.0,
                    }
                },
                "node2": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["Response one"]},
                        "create_time": 1609459210.0,
                    }
                },
            }
        },
        {
            "title": "Chat Two",
            "create_time": 1609545600.0,  # Next day
            "mapping": {
                "node1": {
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": ["Second chat"]},
                        "create_time": 1609545600.0,
                    }
                },
                "node2": {
                    "message": {
                        "author": {"role": "assistant"},
                        "content": {"parts": ["Response two"]},
                        "create_time": 1609545610.0,
                    }
                },
            }
        }
    ]

    json_bytes = json.dumps(data).encode()
    written = llm_chat_importer.import_file(json_bytes, "conversations.json", mock_memories_dir)

    assert len(written) == 2
    assert any("chat-one" in f for f in written)
    assert any("chat-two" in f for f in written)


def test_conversation_body_truncation(mock_memories_dir):
    """Test that conversation body is truncated correctly."""
    # Create a conversation with many long messages
    long_messages = []
    for i in range(10):
        long_messages.append({
            "sender": "human" if i % 2 == 0 else "assistant",
            "text": "A" * 1000,  # Very long message
            "created_at": f"2021-01-01T00:00:{i:02d}Z",
        })

    data = [
        {
            "uuid": "truncate-test",
            "name": "Truncation Test",
            "created_at": "2021-01-01T00:00:00Z",
            "messages": long_messages
        }
    ]

    json_bytes = json.dumps(data).encode()
    written = llm_chat_importer.import_file(json_bytes, "test.json", mock_memories_dir)

    # Read file and check it's not too large
    file_path = mock_memories_dir / written[0]
    content = file_path.read_text()

    # Should be under 4KB (with truncation)
    assert len(content) < 4096

    # Should contain truncation indicators
    assert "..." in content
