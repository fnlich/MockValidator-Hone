import os, subprocess, sys, tempfile
tmp = tempfile.mkdtemp(prefix="chk")
src = os.path.join(tmp, "Check.java")
open(src, "w").write("""import intervals.Intervals;
import java.util.*;
public class Check {
  static void show(int[][] in) {
    List<int[]> l = new ArrayList<>();
    for (int[] x : in) l.add(x);
    StringBuilder sb = new StringBuilder();
    for (int[] x : Intervals.merge(l)) sb.append("[").append(x[0]).append(",").append(x[1]).append("]");
    System.out.println(Arrays.deepToString(in) + " -> " + sb);
  }
  public static void main(String[] a) {
""" + 'show(new int[][]{{1,3},{3,5}}); show(new int[][]{{1,2},{3,4}}); show(new int[][]{{1,4},{2,3}}); show(new int[][]{{5,5},{5,5}}); show(new int[][]{{0,0},{1,1},{0,1}});' + """
  }
}
""")
files = [os.path.join(d, f) for d, _, fs in os.walk("src") for f in fs if f.endswith(".java")]
r = subprocess.run(["javac", "-d", os.path.join(tmp, "out"), src, *files], capture_output=True, timeout=180)
if r.returncode:
    print("build failed")
else:
    r = subprocess.run(["java", "-cp", os.path.join(tmp, "out"), "Check"], capture_output=True, timeout=60)
    sys.stdout.write(r.stdout.decode())
    print("exit", r.returncode)
