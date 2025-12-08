"""Entry point for running the LCP encryption worker.

This module allows running the worker directly using:
    python -m evilflowers_lcpencrypt_worker

Example:
    python -m evilflowers_lcpencrypt_worker
    python -m evilflowers_lcpencrypt_worker -Q custom_queue
    python -m evilflowers_lcpencrypt_worker --queue high_priority --loglevel debug
"""

import argparse
import sys

from evilflowers_lcpencrypt_worker import app, logger


def main() -> int:
    """Main entry point for the Celery worker.

    Returns:
        Exit code (0 for success, non-zero for error).
    """
    parser = argparse.ArgumentParser(
        prog="evilflowers_lcpencrypt_worker",
        description="Celery worker for LCP encryption tasks",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment Variables:
  BROKER                      Celery broker URL (default: redis://localhost:6379/7)
  STORAGE_PATH                Base path for file storage (default: /mnt/data)
  READIUM_LCPENCRYPT_BIN      Path to lcpencrypt binary (default: lcpencrypt)
  LOG_LEVEL                   Logging level (default: INFO)
  OTEL_SERVICE_NAME           Service name for OpenTelemetry
  OTEL_EXPORTER_OTLP_ENDPOINT OpenTelemetry Collector endpoint

Examples:
  python -m evilflowers_lcpencrypt_worker
  python -m evilflowers_lcpencrypt_worker -Q high_priority
  python -m evilflowers_lcpencrypt_worker --queue lcpencrypt --loglevel debug
  python -m evilflowers_lcpencrypt_worker --concurrency 4 --prefetch-multiplier 2
        """,
    )

    parser.add_argument(
        "-Q",
        "--queue",
        type=str,
        default="evilflowers_lcpencrypt_worker",
        help="Queue name to consume tasks from (default: evilflowers_lcpencrypt_worker)",
    )

    parser.add_argument(
        "-l",
        "--loglevel",
        type=str,
        choices=["debug", "info", "warning", "error", "critical"],
        default="info",
        help="Logging level (default: info)",
    )

    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=None,
        help="Number of worker processes/threads (default: number of CPUs)",
    )

    parser.add_argument(
        "-n",
        "--hostname",
        type=str,
        default=None,
        help="Set custom hostname (default: auto-generated)",
    )

    parser.add_argument(
        "--prefetch-multiplier",
        type=int,
        default=1,
        help="Number of tasks to prefetch per worker (default: 1)",
    )

    parser.add_argument(
        "--max-tasks-per-child",
        type=int,
        default=None,
        help="Maximum number of tasks a worker process can execute before being replaced",
    )

    parser.add_argument(
        "--autoscale",
        type=str,
        default=None,
        metavar="MAX,MIN",
        help="Enable autoscaling with max,min workers (e.g., 10,3)",
    )

    parser.add_argument(
        "-P",
        "--pool",
        type=str,
        choices=["prefork", "eventlet", "gevent", "solo", "threads"],
        default="prefork",
        help="Pool implementation (default: prefork)",
    )

    parser.add_argument(
        "--without-heartbeat",
        action="store_true",
        help="Disable heartbeat (not recommended for production)",
    )

    parser.add_argument(
        "--without-gossip",
        action="store_true",
        help="Disable gossip (not recommended for production)",
    )

    parser.add_argument(
        "--without-mingle",
        action="store_true",
        help="Disable mingle at startup",
    )

    args = parser.parse_args()

    logger.info(f"Starting Celery worker for queue: {args.queue}")
    logger.info(f"Broker: {app.conf.broker_url}")

    # Build worker arguments
    worker_args = [
        f"--loglevel={args.loglevel}",
        f"--queues={args.queue}",
        f"--pool={args.pool}",
        f"--prefetch-multiplier={args.prefetch_multiplier}",
    ]

    if args.concurrency is not None:
        worker_args.append(f"--concurrency={args.concurrency}")

    if args.hostname:
        worker_args.append(f"--hostname={args.hostname}")

    if args.max_tasks_per_child:
        worker_args.append(f"--max-tasks-per-child={args.max_tasks_per_child}")

    if args.autoscale:
        worker_args.append(f"--autoscale={args.autoscale}")

    if args.without_heartbeat:
        worker_args.append("--without-heartbeat")

    if args.without_gossip:
        worker_args.append("--without-gossip")

    if args.without_mingle:
        worker_args.append("--without-mingle")

    try:
        # Start the worker (prepend 'worker' command)
        app.worker_main(argv=["worker"] + worker_args)
        return 0
    except KeyboardInterrupt:
        logger.info("Worker shutdown requested by user")
        return 0
    except Exception as e:
        logger.error(f"Worker failed to start: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
