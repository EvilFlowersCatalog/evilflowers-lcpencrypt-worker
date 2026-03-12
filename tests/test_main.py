"""Tests for the __main__ module (CLI entry point)."""

from unittest.mock import MagicMock, patch

from evilflowers_lcpencrypt_worker.__main__ import main


class TestMain:
    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_default_arguments(self, mock_app):
        """Test that default arguments are passed correctly."""
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog"]):
            result = main()

        assert result == 0
        call_args = mock_app.worker_main.call_args
        worker_args = call_args.kwargs.get("argv") or call_args[1].get("argv")
        assert "worker" in worker_args
        assert "--queues=evilflowers_lcpencrypt_worker" in worker_args
        assert "--loglevel=info" in worker_args
        assert "--pool=prefork" in worker_args
        assert "--prefetch-multiplier=1" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_custom_queue(self, mock_app):
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog", "-Q", "high_priority"]):
            main()

        worker_args = mock_app.worker_main.call_args.kwargs.get("argv") or mock_app.worker_main.call_args[1].get(
            "argv"
        )
        assert "--queues=high_priority" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_custom_loglevel(self, mock_app):
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog", "-l", "debug"]):
            main()

        worker_args = mock_app.worker_main.call_args.kwargs.get("argv") or mock_app.worker_main.call_args[1].get(
            "argv"
        )
        assert "--loglevel=debug" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_concurrency(self, mock_app):
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog", "-c", "4"]):
            main()

        worker_args = mock_app.worker_main.call_args.kwargs.get("argv") or mock_app.worker_main.call_args[1].get(
            "argv"
        )
        assert "--concurrency=4" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_hostname(self, mock_app):
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog", "-n", "worker-1"]):
            main()

        worker_args = mock_app.worker_main.call_args.kwargs.get("argv") or mock_app.worker_main.call_args[1].get(
            "argv"
        )
        assert "--hostname=worker-1" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_autoscale(self, mock_app):
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog", "--autoscale", "10,3"]):
            main()

        worker_args = mock_app.worker_main.call_args.kwargs.get("argv") or mock_app.worker_main.call_args[1].get(
            "argv"
        )
        assert "--autoscale=10,3" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_max_tasks_per_child(self, mock_app):
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog", "--max-tasks-per-child", "1000"]):
            main()

        worker_args = mock_app.worker_main.call_args.kwargs.get("argv") or mock_app.worker_main.call_args[1].get(
            "argv"
        )
        assert "--max-tasks-per-child=1000" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_without_heartbeat(self, mock_app):
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog", "--without-heartbeat"]):
            main()

        worker_args = mock_app.worker_main.call_args.kwargs.get("argv") or mock_app.worker_main.call_args[1].get(
            "argv"
        )
        assert "--without-heartbeat" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_without_gossip(self, mock_app):
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog", "--without-gossip"]):
            main()

        worker_args = mock_app.worker_main.call_args.kwargs.get("argv") or mock_app.worker_main.call_args[1].get(
            "argv"
        )
        assert "--without-gossip" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_without_mingle(self, mock_app):
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog", "--without-mingle"]):
            main()

        worker_args = mock_app.worker_main.call_args.kwargs.get("argv") or mock_app.worker_main.call_args[1].get(
            "argv"
        )
        assert "--without-mingle" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_pool_selection(self, mock_app):
        mock_app.worker_main = MagicMock()

        with patch("sys.argv", ["prog", "-P", "solo"]):
            main()

        worker_args = mock_app.worker_main.call_args.kwargs.get("argv") or mock_app.worker_main.call_args[1].get(
            "argv"
        )
        assert "--pool=solo" in worker_args

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_keyboard_interrupt_returns_0(self, mock_app):
        mock_app.worker_main.side_effect = KeyboardInterrupt()

        with patch("sys.argv", ["prog"]):
            result = main()

        assert result == 0

    @patch("evilflowers_lcpencrypt_worker.__main__.app")
    def test_exception_returns_1(self, mock_app):
        mock_app.worker_main.side_effect = RuntimeError("startup failure")

        with patch("sys.argv", ["prog"]):
            result = main()

        assert result == 1