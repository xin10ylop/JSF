"""How much RAM does the whole bot set REALLY need?

Summing RSS across processes overstates the answer, sometimes badly. Five
python processes import the same interpreter, the same numpy shared
objects and the same OpenSSL; those pages exist once in physical memory
but appear in every process's RSS. Sizing a host on that arithmetic buys
capacity that is never used.

PSS (proportional set size) divides each shared page by the number of
processes sharing it, so summing PSS across a group gives the group's true
footprint. That is also, near enough, what a systemd slice's memory.current
will charge -- which is what actually decides whether a box swaps.

    python3 src/mem_true.py
"""
import glob
import os


def ours(pid_dir):
    try:
        argv = open(f"{pid_dir}/cmdline", "rb").read().decode(
            "utf-8", "replace").split("\x00")
    except OSError:
        return None
    argv = [x for x in argv if x]
    if not argv or "python" not in os.path.basename(argv[0]):
        return None
    if not any(x.endswith(("bot/run.py", "bot/recorder.py")) for x in argv):
        return None
    if "recorder" in " ".join(argv):
        return "recorder"
    return ("bot " + " ".join(argv).split("--coin")[-1].strip()
            if "--coin" in " ".join(argv) else "bot")


def rollup(pid_dir):
    out = {}
    try:
        for line in open(f"{pid_dir}/smaps_rollup"):
            k, _, v = line.partition(":")
            if k in ("Rss", "Pss", "Private_Clean", "Private_Dirty"):
                out[k] = int(v.strip().split()[0]) / 1024.0   # MB
    except OSError:
        return None
    return out


def main():
    rows = []
    for d in sorted(glob.glob("/proc/[0-9]*")):
        label = ours(d)
        if not label:
            continue
        m = rollup(d)
        if not m:
            continue
        rows.append((label, m.get("Rss", 0), m.get("Pss", 0),
                     m.get("Private_Clean", 0) + m.get("Private_Dirty", 0)))
    if not rows:
        print("no bot/run.py or bot/recorder.py processes running.")
        return
    rows.sort()
    print(f"{'process':<16}{'RSS MB':>9}{'PSS MB':>9}{'private MB':>12}")
    tr = tp = tv = 0.0
    for label, r, p, v in rows:
        tr += r
        tp += p
        tv += v
        print(f"{label:<16}{r:>9.0f}{p:>9.0f}{v:>12.0f}")
    print(f"{'TOTAL':<16}{tr:>9.0f}{tp:>9.0f}{tv:>12.0f}")
    print(f"\nSumming RSS overstates the group by {tr - tp:,.0f} MB "
          f"({(tr - tp) / tr:.0%}) -- that is shared library and "
          f"interpreter text counted {len(rows)} times.")
    print(f"True footprint of the set: {tp:,.0f} MB")
    for name, ram in (("1 GB ($6)", 961), ("2 GB ($12/$16)", 1963)):
        # ~150 MB for sshd/systemd/journald, ~300 MB peak for the daily
        # edge check holding pandas and parquet on top of steady state.
        free_steady = ram - 150 - tp
        free_peak = free_steady - 300
        print(f"  on {name:<16} {free_steady:>5.0f} MB spare steady, "
              f"{free_peak:>5.0f} MB spare during the daily edge check")


if __name__ == "__main__":
    main()
