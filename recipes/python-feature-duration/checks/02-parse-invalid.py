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
for t in ["", "1h 30m", "h", "30", "1m1h", "1s1s", "1d", "٣s", "-5s", " 5s"]:
    show(t)
