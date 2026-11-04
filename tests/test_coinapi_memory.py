from examples.coinapi_replay.memory import ProcessMemory, ProcessMemorySampler


def test_memory_sampler_retains_bounded_initial_peak_final_counters():
    values = iter(
        (
            ProcessMemory(100, 80, 150, "committed"),
            ProcessMemory(120, 60, 160, "committed"),
            ProcessMemory(90, 70, 160, "committed"),
        )
    )
    sampler = ProcessMemorySampler(123, reader=lambda pid: next(values))
    for _ in range(3):
        sampler.sample()
    result = sampler.summary()
    assert result["samples"] == 3
    assert result["private_kind"] == "committed"
    assert result["initial"]["working_set_bytes"] == 100
    assert result["final"]["working_set_bytes"] == 90
    assert result["sampled_peak"]["working_set_bytes"] == 120
    assert result["sampled_peak"]["private_bytes"] == 80
    assert result["sampled_peak"]["os_peak_working_set_bytes"] == 160
    result["initial"]["working_set_bytes"] = -1
    assert sampler.summary()["initial"]["working_set_bytes"] == 100


def test_memory_read_failure_is_explicit_without_erasing_last_sample():
    calls = 0

    def reader(pid):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("process exited")
        return ProcessMemory(100, 80, 150, "committed")

    sampler = ProcessMemorySampler(123, reader=reader)
    sampler.sample()
    sampler.sample()
    result = sampler.summary()
    assert result["samples"] == result["failed_samples"] == 1
    assert result["error"] == "OSError: process exited"
    assert result["final"]["private_bytes"] == 80
