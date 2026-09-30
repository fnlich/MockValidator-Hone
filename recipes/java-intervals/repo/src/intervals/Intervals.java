package intervals;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;

/**
 * Merges closed integer intervals.
 *
 * <p>{@code merge} accepts intervals in any order ({@code int[]{start, end}},
 * start <= end) and returns the minimal list of disjoint intervals covering
 * the same points, sorted by start. Intervals that overlap OR touch are merged:
 * [1,3] and [3,5] become [1,5]. Adjacent integers do not touch: [1,2] and [3,4]
 * stay separate. The input list is not modified.
 */
public final class Intervals {
    private Intervals() {}

    public static List<int[]> merge(List<int[]> input) {
        List<int[]> sorted = new ArrayList<>();
        for (int[] iv : input) {
            sorted.add(new int[] {iv[0], iv[1]});
        }
        sorted.sort(Comparator.comparingInt((int[] iv) -> iv[0]).thenComparingInt(iv -> iv[1]));
        List<int[]> out = new ArrayList<>();
        for (int[] iv : sorted) {
            if (!out.isEmpty() && iv[0] <= out.get(out.size() - 1)[1]) {
                int[] last = out.get(out.size() - 1);
                last[1] = Math.max(last[1], iv[1]);
            } else {
                out.add(iv);
            }
        }
        return out;
    }
}
