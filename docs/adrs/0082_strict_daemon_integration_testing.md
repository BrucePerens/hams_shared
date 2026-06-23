# ADR 0082: Strict Daemon Integration Testing

## Context
Historically, Odoo integration tests for external daemons (like `cache_manager` and `backup_worker`) relied heavily on `unittest.mock` and `patch` to simulate subprocess execution, RabbitMQ message consumption, or Redis caching. This resulted in tests that verified the *mocks* rather than the actual robustness of the deployed services, masking configuration drifts and failing to capture race conditions in inter-process communication.

## Decision
We mandate a **Zero-Mock Policy for Daemon Testing**:
1. **Unified Testing Mode:** We have removed the distinction between `standard` and `integration` test modes. All tests run against live, fully provisioned background services (Odoo HTTP, PostgreSQL, RabbitMQ, Redis).
2. **Real Subprocess Execution:** Tests must launch the actual daemon Python scripts using `HamsTransactionCase.start_daemon()` and verify their real-world side effects.
3. **Live Message Queues:** Tests verifying RabbitMQ or Redis functionality MUST connect to the live message brokers, publish real payloads, and assert that the daemon successfully consumed and processed the messages via observable state changes.
4. **Prohibition of Patching:** Using `@patch` to mock `subprocess.Popen`, `pika.BlockingConnection`, or `redis.Redis` inside daemon integration tests is strictly forbidden.

## Consequences
* **Pros:** Complete confidence that the daemons work seamlessly with the actual infrastructure. Catching network timeouts, serialization bugs, and message format drifts before production.
* **Cons:** Tests may take slightly longer to run due to actual daemon spin-up times and message queue polling, requiring resilient `time.sleep()` backoffs in tests.
