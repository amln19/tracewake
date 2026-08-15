"""RootSE adapter: three action encodings, and what counts as a write.

Write detection is what decides whether the commitment rule has anything to point at on
an imported trace, so it is tested per encoding rather than end to end.
"""

from __future__ import annotations

from bench.rootse import decode_action, to_steps


def test_openhands_structured_editor_write():
    name, _, target, writes = decode_action(
        {"tool": "str_replace_editor",
         "input": {"command": "str_replace", "path": "/w/a.py", "new_str": "x"}}
    )
    assert name == "str_replace_editor.str_replace"
    assert target == "/w/a.py"
    assert writes == {"/w/a.py"}


def test_openhands_view_is_not_a_write():
    _, _, _, writes = decode_action(
        {"tool": "str_replace_editor", "input": {"command": "view", "path": "/w/a.py"}}
    )
    assert writes == set()


def test_swe_agent_positional_editor_form():
    # SWE-agent drives the same editor through a CLI: `<command> <path>`.
    name, _, target, writes = decode_action("str_replace_editor create /w/new.py")
    assert name == "str_replace_editor.create"
    assert target == "/w/new.py"
    assert writes == {"/w/new.py"}

    name, _, _, writes = decode_action("str_replace_editor view /w/new.py")
    assert name == "str_replace_editor.view"
    assert writes == set()


def test_auto_code_rover_call_list():
    name, args, _, writes = decode_action(
        [{"func_name": "search_method_in_file",
          "arguments": {"method_name": "f", "file_name": "a.py"}, "call_ok": True}]
    )
    assert name == "search_method_in_file"
    assert args["file_name"] == "a.py"
    assert writes == set()
    # write_patch carries no path, so it cannot anchor a commitment to a file.
    _, _, _, writes = decode_action([{"func_name": "write_patch", "arguments": {}}])
    assert writes == {"<patch>"}


def test_bash_redirect_and_in_place_edit_are_writes():
    assert decode_action("echo hi > /w/out.txt")[3] == {"/w/out.txt"}
    assert decode_action("sed -i 's/a/b/' /w/a.py")[3] == {"/w/a.py"}
    assert decode_action("cat x | tee /w/log.txt")[3] == {"/w/log.txt"}
    assert decode_action("python3 editor.py replace /w/a.py")[3] == {"/w/a.py"}


def test_bash_reads_and_stream_redirection_are_not_writes():
    assert decode_action("grep -rn foo /w/src")[3] == set()
    assert decode_action("pytest tests/ 2>&1")[3] == set()
    assert decode_action("ls -la /w > /dev/null")[3] == set()


def test_a_step_with_no_action_is_kept_so_label_indices_stay_aligned():
    # RootSE labels index into the raw trajectory, so a reasoning-only step
    # must still occupy a position.
    steps = to_steps(
        [
            {"action": "ls /w", "thought": "look"},
            {"action": "", "response": "thinking out loud"},
            {"action": "str_replace_editor create /w/a.py", "thought": "write"},
        ]
    )
    assert len(steps) == 3
    assert steps[1].name == "(no-op)"
    assert steps[2].writes == frozenset({"/w/a.py"})


def test_terminal_actions_are_stripped_only_on_request():
    trajectory = [{"action": "ls /w"}, {"action": "submit"}]
    assert len(to_steps(trajectory)) == 2
    assert len(to_steps(trajectory, strip_terminal=True)) == 1


def test_arrows_and_comparisons_are_not_redirects():
    """`node -e` and `python -c` payloads carry `=>` and `>=`.

    Reading those as shell redirects invented a write — and a write invents a
    commitment, which moves the reported divergence earlier.
    """
    assert decode_action('cd /app && node -e "arr.filter(v => v !== 1)"')[3] == set()
    assert decode_action('python -c "print(1 >= 2)"')[3] == set()
    assert decode_action("grep -rn 'a->b' /w/src")[3] == set()


def test_heredoc_bodies_are_not_scanned_for_redirects():
    # The body of `cat <<EOF > file` is file content, not shell syntax. Scanning
    # it finds every `>` in the payload — diff markers, HTML, arrow functions.
    command = "cat <<'EOF' > /tmp/fix.patch\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\nEOF"
    assert decode_action(command)[3] == {"/tmp/fix.patch"}


def test_redirect_targets_must_look_like_paths():
    from bench.rootse import _bash_writes

    assert _bash_writes('echo x > ")') == set()
    assert _bash_writes("echo x > out.txt") == {"out.txt"}


def test_a_rejected_edit_is_not_a_write():
    from bench.rootse import wrote_nothing

    assert wrote_nothing("ERROR: No replacement was performed, old_str did not appear")
    assert wrote_nothing("<returncode>1</returncode>")
    assert not wrote_nothing("The file /w/a.py has been edited")
    assert not wrote_nothing("<returncode>0</returncode>")

    failed = to_steps(
        [{"action": "str_replace_editor str_replace /w/a.py",
          "observation": "ERROR: No replacement was performed"}]
    )
    assert failed[0].writes == frozenset()

    ok = to_steps(
        [{"action": "str_replace_editor str_replace /w/a.py",
          "observation": "The file /w/a.py has been edited"}]
    )
    assert ok[0].writes == frozenset({"/w/a.py"})
