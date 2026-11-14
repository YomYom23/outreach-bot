from outreach.tls import drop_keylog_pipe, use_os_trust_store


def test_named_pipe_keylog_is_removed() -> None:
    env = {"SSLKEYLOGFILE": "\\\\.\\nllMonFltProxy\\abc", "OTHER": "x"}
    assert drop_keylog_pipe(env) is True
    assert env == {"OTHER": "x"}


def test_real_keylog_file_is_kept() -> None:
    env = {"SSLKEYLOGFILE": "C:\\Users\\me\\keys.log"}
    assert drop_keylog_pipe(env) is False
    assert env == {"SSLKEYLOGFILE": "C:\\Users\\me\\keys.log"}


def test_missing_keylog_is_fine() -> None:
    assert drop_keylog_pipe({}) is False


def test_use_os_trust_store_cleans_env_then_injects() -> None:
    env = {"SSLKEYLOGFILE": "\\\\.\\pipe\\x"}
    seen: list[dict[str, str]] = []
    use_os_trust_store(env, inject=lambda: seen.append(dict(env)))
    assert seen == [{}]
