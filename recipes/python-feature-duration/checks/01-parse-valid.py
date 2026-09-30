import sys
sys.path.insert(0, ".")
try:
    from durations import parse_duration, format_duration
except ImportError as e:
    print("import error", e); raise SystemExit(0)
def show(t):
    try:
        print(repr(t), "->", parse_duration(t))
    except ValueError as e:
        print(repr(t), "-> ValueError", e)
for t in ["1h30m", "45s", "2h5s", "10m", "0s", "1h0m1s", "100h"]:
    show(t)
print(all(parse_duration(format_duration(n)) == n for n in range(0, 20000, 7)))
