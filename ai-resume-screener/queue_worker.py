import os


def main() -> int:
    redis_url = (os.environ.get("REDIS_URL") or "").strip()
    if not redis_url:
        print("queue_worker: REDIS_URL not set; worker disabled.")
        return 0

    # Import inside main to avoid import costs when worker is disabled.
    import redis
    from rq import Worker, Queue, Connection

    listen = (os.environ.get("RQ_QUEUES") or "default").split(",")
    listen = [q.strip() for q in listen if q.strip()]

    conn = redis.from_url(redis_url)

    with Connection(conn):
        worker = Worker([Queue(name) for name in listen])
        print(f"queue_worker: listening on {listen} via REDIS_URL")
        worker.work(with_scheduler=False)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

