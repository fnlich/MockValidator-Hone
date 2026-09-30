import sys
sys.path.insert(0, ".")
import stats
def show(label, fn, *args):
    try:
        v = fn(*args)
        print(label, "->", round(v, 6) if isinstance(v, float) else v)
    except Exception as e:
        print(label, "->", type(e).__name__, e)
data = [15, 20, 35, 40, 50]
for p in (0, 25, 40, 50, 75, 90, 100, 101, -1):
    show("p%r" % (p,), stats.percentile, data, p)
show("p50 two", stats.percentile, [1, 2], 50)
show("p99 one", stats.percentile, [7], 99)
show("empty", stats.percentile, [], 50)
