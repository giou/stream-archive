import json
import os

from stream_archive.chat_writer import ChatJsonWriter, file_info


def comment(i):
    return {"_id": f"m{i}", "message": {"body": f"hello {i}"}}


def test_writes_comments_and_trailer(tmp_path):
    path = str(tmp_path / "chat.json")
    writer = ChatJsonWriter(path)

    assert writer.add_comment(comment(1)) is True
    assert writer.add_comment(comment(2)) is True
    assert writer.close({"video": {"end": 10.0}, "FileInfo": file_info()}) is True

    with open(path) as f:
        data = json.load(f)
    assert [c["_id"] for c in data["comments"]] == ["m1", "m2"]
    assert data["video"] == {"end": 10.0}
    assert data["FileInfo"]["Version"] == {"Major": 1, "Minor": 4, "Patch": 0}
    assert writer.comments == 2
    assert not (tmp_path / "chat.json.tmp").exists()


def test_comments_land_on_disk_before_close(tmp_path):
    path = str(tmp_path / "chat.json")
    writer = ChatJsonWriter(path)

    writer.add_comment(comment(1))
    assert '"m1"' in (tmp_path / "chat.json.tmp").read_text()
    assert not (tmp_path / "chat.json").exists()

    writer.add_comment(comment(2))
    assert '"m2"' in (tmp_path / "chat.json.tmp").read_text()

    writer.close({})
    with open(path) as f:
        data = json.load(f)
    assert [c["_id"] for c in data["comments"]] == ["m1", "m2"]


def test_close_without_comments_writes_empty_array(tmp_path):
    path = str(tmp_path / "chat.json")
    writer = ChatJsonWriter(path)

    assert writer.close({"FileInfo": file_info()}) is True

    with open(path) as f:
        data = json.load(f)
    assert data["comments"] == []
    assert data["FileInfo"]["CreatedAt"].endswith("Z")


def test_trailer_comments_key_is_skipped(tmp_path, caplog):
    path = str(tmp_path / "chat.json")
    writer = ChatJsonWriter(path)
    writer.add_comment(comment(1))

    with caplog.at_level("ERROR", logger="stream_archive.chat_writer"):
        assert writer.close({"comments": [comment(9)], "FileInfo": file_info()}) is True

    with open(path) as f:
        data = json.load(f)
    assert [c["_id"] for c in data["comments"]] == ["m1"]  # the trailer copy stays out
    assert any("collides" in r.getMessage() for r in caplog.records)


def test_discard_removes_tmp_and_leaves_no_file(tmp_path):
    path = str(tmp_path / "chat.json")
    writer = ChatJsonWriter(path)
    writer.add_comment(comment(1))

    writer.discard()

    assert not (tmp_path / "chat.json").exists()
    assert not (tmp_path / "chat.json.tmp").exists()


def test_open_failure_reports_once_and_never_writes(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    errors = []

    writer = ChatJsonWriter(str(blocker / "chat.json"), on_error=errors.append)

    assert writer.failed is True
    assert len(errors) == 1
    assert isinstance(errors[0], FileExistsError)  # the makedirs failure itself
    assert writer.add_comment(comment(1)) is False
    assert writer.close({"FileInfo": file_info()}) is False
    assert len(errors) == 1  # neither call reports again
    assert not (blocker / "chat.json").exists()


def test_write_failure_keeps_partial_file(tmp_path):
    path = str(tmp_path / "chat.json")
    errors = []
    writer = ChatJsonWriter(path, on_error=errors.append)
    writer.add_comment(comment(1))
    real = writer._fh

    class FailingHandle:
        def write(self, text):
            msg = "No space left on device"
            raise OSError(msg)

        def __getattr__(self, name):
            return getattr(real, name)

    writer._fh = FailingHandle()

    assert writer.add_comment(comment(2)) is False
    assert writer.failed is True
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)
    assert "No space left on device" in str(errors[0])
    assert writer.add_comment(comment(3)) is False
    assert len(errors) == 1  # the handler runs exactly once, not per message
    # The partial file stays for recovery. The target path stays untouched.
    assert (tmp_path / "chat.json.tmp").exists()
    assert '"m1"' in (tmp_path / "chat.json.tmp").read_text()
    assert not (tmp_path / "chat.json").exists()


def test_tell_failure_marks_the_writer_failed(tmp_path):
    path = str(tmp_path / "chat.json")
    errors = []
    writer = ChatJsonWriter(path, on_error=errors.append)
    real = writer._fh

    class BadTellHandle:
        def tell(self):
            msg = "tell failed"
            raise OSError(msg)

        def __getattr__(self, name):
            return getattr(real, name)

    writer._fh = BadTellHandle()

    assert writer.add_comment(comment(1)) is False  # a tell() fault must not raise
    assert writer.failed is True
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)
    assert "tell failed" in str(errors[0])


def test_close_failure_keeps_tmp_and_no_target(tmp_path, monkeypatch):
    path = str(tmp_path / "chat.json")
    errors = []
    writer = ChatJsonWriter(path, on_error=errors.append)
    writer.add_comment(comment(1))

    real_replace = os.replace

    def boom(src, dst):
        # Fail only the writer rename, so unrelated renames keep working.
        if src != writer.tmp_path:
            return real_replace(src, dst)
        msg = "rename failed"
        raise OSError(msg)

    monkeypatch.setattr("stream_archive.chat_writer.os.replace", boom)

    assert writer.close({"FileInfo": file_info()}) is False
    assert writer.failed is True
    assert len(errors) == 1
    assert isinstance(errors[0], OSError)
    assert "rename failed" in str(errors[0])
    assert (tmp_path / "chat.json.tmp").exists()
    assert not (tmp_path / "chat.json").exists()
