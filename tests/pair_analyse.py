"""Compare the logs of a host and a friend run: how far apart were the films, phase by phase?"""
import bisect
import json
import sys


def load(path):
    samples, events, extra = [], [], []
    for line in open(path):
        entry = json.loads(line)
        if "pos" in entry:
            samples.append((entry["t"], entry["pos"], entry["playing"]))
        elif "event" in entry and "t" in entry:
            events.append((entry["t"], entry["event"]))
        else:
            extra.append(entry)
    return samples, events, extra


def position_at(samples, t):
    times = [s[0] for s in samples]
    i = bisect.bisect_left(times, t)
    if i <= 0 or i >= len(samples):
        return None
    (t0, p0, _), (t1, p1, _) = samples[i - 1], samples[i]
    return p0 + (p1 - p0) * (t - t0) / (t1 - t0) if t1 > t0 else p0


host, host_events, host_extra = load(sys.argv[1])
friend, friend_events, friend_extra = load(sys.argv[2])
events = sorted(host_events + friend_events)
print("events:", [(round(t - events[0][0], 1), e) for t, e in events])
start = max(host[0][0], friend[0][0])
end = min(host[-1][0], friend[-1][0])
t = start
window = []
while t < end:
    h, f = position_at(host, t), position_at(friend, t)
    if h is not None and f is not None:
        window.append((t - events[0][0], h - f))
    t += 0.5
for t, gap in window:
    if int(t * 2) % 2 == 0:
        print(f"t={t:5.1f}s  host-friend gap = {gap:8.0f} ms")
for entry in host_extra + friend_extra:
    print(entry)
