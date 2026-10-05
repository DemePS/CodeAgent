from types import SimpleNamespace

from coding_agent import usage


def reply(i, o, read=0, write=0):
    return SimpleNamespace(usage=SimpleNamespace(input_tokens=i, output_tokens=o, cache_read_input_tokens=read,
                                                  cache_creation_input_tokens=write))


def test_calls_are_added_by_kind_and_since_gives_the_difference():
    usage.reset()
    usage.record(reply(10, 5), "agent")
    mark = usage.snapshot()
    usage.record(reply(100, 20, read=50, write=7), "agent")
    usage.record(reply(3, 1), "memory")
    usage.record(SimpleNamespace(), "agent")  # no usage on it: ignored
    used = usage.since(mark)["calls"]
    assert used["agent"] == {"input": 100, "output": 20, "cache_read": 50, "cache_write": 7, "calls": 1}
    assert used["memory"]["input"] == 3 and "compact" not in used
    assert usage.snapshot()["calls"]["agent"]["calls"] == 2


def test_tool_results_are_estimated_per_tool(tmp_path):
    usage.reset()
    mark = usage.snapshot()
    usage.record_tool("read_file", "x" * 3500)
    usage.record_tool("read_file", "x" * 350)
    assert usage.since(mark)["tools"] == {"read_file": {"calls": 2, "result_tokens": 1100}}


def test_compaction_records_its_call():
    from coding_agent import context
    usage.reset()

    class Client:
        class messages:
            @staticmethod
            def create(**kw):
                r = reply(4, 2)
                r.content, r.stop_reason = [SimpleNamespace(type="text", text="<summary>s</summary>")], "end_turn"
                return r
    context.summarize_history(Client, [{"role": "user", "content": "hello"}])
    assert usage.snapshot()["calls"]["compact"]["calls"] == 1


def test_a_line_sums_the_calls_and_is_empty_when_nothing_was_used():
    usage.reset()
    mark = usage.snapshot()
    assert usage.line(usage.since(mark)) == ""
    usage.record(reply(10, 5, read=100, write=20), "agent")
    usage.record(reply(1, 1), "compact")
    assert usage.line(usage.since(mark)) == "tokens: 131 in (100 cached), 6 out, 2 call(s)"
