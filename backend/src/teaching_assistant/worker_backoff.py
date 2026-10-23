"""Bound infrastructure retries independently of task/model retry budgets."""


def failure_delay(poll_seconds: float, failures: int) -> float:
    # Keep configured slow polling, cap exponential growth, and never charge a
    # task attempt merely because the database cannot be reached.
    return min(max(poll_seconds, 0.25) * 2 ** min(max(failures - 1, 0), 4), max(poll_seconds, 2.0))
