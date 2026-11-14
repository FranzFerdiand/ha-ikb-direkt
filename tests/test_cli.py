"""CLI parsing regression without contacting IKB."""
from unittest.mock import AsyncMock, patch
import pytest
import cli


@pytest.mark.parametrize("args", [
    ["test-login", "-u", "example", "-p", "dummy"],
    ["-u", "example", "-p", "dummy", "test-login"],
])
def test_credentials_before_and_after_subcommand(args):
    handler = AsyncMock(return_value=0)
    with patch("sys.argv", ["cli.py", *args]), patch.object(cli, "cmd_test_login", handler):
        with pytest.raises(SystemExit) as exc:
            cli.main()
    assert exc.value.code == 0
    namespace = handler.call_args.args[0]
    assert (namespace.username, namespace.password) == ("example", "dummy")
