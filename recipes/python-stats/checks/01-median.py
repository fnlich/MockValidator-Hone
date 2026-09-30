import sys
sys.path.insert(0, ".")
import stats
def show(label, fn, *args):
    try:
        v = fn(*args)
        print(label, "->", round(v, 6) if isinstance(v, float) else v)
    except Exception as e:
        print(label, "->", type(e).__name__, e)
for data in ([3, 1, 2], [4, 1, 3, 2], [10, 20], [5], [], [1.5, 2.5, 3.5, 4.5], [-3, -1, -2, -4]):
    show("median %r" % (data,), stats.median, data)
