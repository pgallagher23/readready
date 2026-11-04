// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

/** Unit tests for the fit-to-bbox layout helpers (no PDF required). */
class FitToBboxLayoutTest {

    private static String[] words(int n) {
        String[] w = new String[n];
        for (int i = 0; i < n; i++) w[i] = "wordy"; // 5 chars each
        return w;
    }

    @Test
    void singleShortWordIsOneLine() {
        assertEquals(1, PdfAccessibilityTagger.layoutLineCount(
            new String[]{"hello"}, 200f, 10f));
    }

    @Test
    void narrowColumnWrapsEveryWord() {
        // 6pt-wide column, 5-char words ~ 5*10*0.55 = 27.5pt each -> wraps each.
        int lines = PdfAccessibilityTagger.layoutLineCount(words(10), 6f, 10f);
        assertEquals(10, lines);
    }

    @Test
    void wideColumnFitsManyWordsPerLine() {
        // 500pt wide, 5-char words at 10pt -> ~16 words/line, 32 words -> ~2-3 lines.
        int lines = PdfAccessibilityTagger.layoutLineCount(words(32), 500f, 10f);
        assertTrue(lines >= 2 && lines <= 4, "got " + lines);
    }

    @Test
    void smallerFontFitsMorePerLine() {
        int big = PdfAccessibilityTagger.layoutLineCount(words(40), 200f, 12f);
        int small = PdfAccessibilityTagger.layoutLineCount(words(40), 200f, 4f);
        assertTrue(small < big, "smaller font should need fewer lines: "
            + small + " vs " + big);
    }

    @Test
    void comfortableBboxReturnsFittingSize() {
        String[] w = words(40); // 40 * "wordy"
        float fs = PdfAccessibilityTagger.computeFitFontSize(w, 466f, 120f, 2f, 48f);
        // Must be within bounds and actually fit the height.
        assertTrue(fs >= 2f && fs <= 48f, "size out of range: " + fs);
        float used = PdfAccessibilityTagger.layoutLineCount(w, 466f, fs) * fs * 1.15f;
        assertTrue(used <= 120f + 0.01f, "layout overflows bbox: used=" + used);
    }

    @Test
    void degenerateSliverReturnsFloorAndDoesNotFit() {
        String[] w = words(60);
        float fs = PdfAccessibilityTagger.computeFitFontSize(w, 6f, 58f, 2f, 48f);
        assertEquals(2f, fs, 0.0001f);
        float used = PdfAccessibilityTagger.layoutLineCount(w, 6f, fs) * fs * 1.15f;
        assertTrue(used > 58f, "sliver should NOT fit at floor: used=" + used);
    }

    @Test
    void tinyTextInHugeBboxReturnsMax() {
        float fs = PdfAccessibilityTagger.computeFitFontSize(
            new String[]{"hi"}, 500f, 700f, 2f, 48f);
        assertEquals(48f, fs, 0.5f);
    }
}
