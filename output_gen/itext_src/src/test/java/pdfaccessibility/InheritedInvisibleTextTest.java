// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.charset.StandardCharsets;

import org.junit.jupiter.api.Test;

/**
 * Text render mode is graphics state: a "3 Tr" set in one text block stays in
 * effect for later blocks until changed or restored by Q.  JSTOR scans set it
 * once in an otherwise empty block, so stripping must follow that state, not
 * just look for "3 Tr" inside each block.
 */
class InheritedInvisibleTextTest {

    private static String strip(String content) {
        return new String(PdfAccessibilityTagger.stripInvisibleText(
            content.getBytes(StandardCharsets.ISO_8859_1)), StandardCharsets.ISO_8859_1);
    }

    @Test
    void textInheritingInvisibleModeIsStripped() {
        String out = strip("q\n/img0 Do\nQ\n"
            + "BT\n1 0 0 1 0 0 Tm\n3 Tr\nET\n"
            + "q\nBT\n/F1 10 Tf\n1 0 0 1 72 700 Tm\n(hidden OCR) Tj\nET\nQ\n");
        assertFalse(out.contains("(hidden OCR)"), out);
        assertTrue(out.contains("/img0 Do"), out);
    }

    private static int count(String s, String op) {
        return (int) java.util.Arrays.stream(s.split("\\s+")).filter(op::equals).count();
    }

    @Test
    void strippingKeepsSaveRestorePairsBalanced() {
        // JSTOR wraps every OCR block in q..Q; removing the block must not
        // leave a Q without its q (viewers report a page error).
        String out = strip("BT\n3 Tr\nET\n"
            + "q\nBT\n/F1 10 Tf\n(one) Tj\nET\nQ\n"
            + "q\nBT\n/F1 10 Tf\n(two) Tj\nET\nQ\n");
        assertFalse(out.contains("(one)") || out.contains("(two)"), out);
        org.junit.jupiter.api.Assertions.assertEquals(count(out, "q"), count(out, "Q"), out);
    }

    @Test
    void modeSetInsideQIsRestoredByQ() {
        String out = strip("q\nBT\n3 Tr\nET\nQ\nBT\n/F1 10 Tf\n(visible) Tj\nET\n");
        assertTrue(out.contains("(visible) Tj"), out);
    }

    @Test
    void laterZeroTrMakesTextVisibleAgain() {
        String out = strip("BT\n3 Tr\nET\nBT\n/F1 10 Tf\n0 Tr\n(visible) Tj\nET\n");
        assertTrue(out.contains("(visible) Tj"), out);
    }

    @Test
    void operatorWordsInsideStringsAreIgnored() {
        // A "Q" inside a string must not be read as a restore of the mode.
        String out = strip("BT\n3 Tr\nET\nBT\n/F1 10 Tf\n(a Q BT q) Tj\nET\n"
            + "BT\n/F1 10 Tf\n(more hidden) Tj\nET\n");
        assertFalse(out.contains("(a Q BT q)"), out);
        assertFalse(out.contains("(more hidden)"), out);
    }

    @Test
    void laterStrokedTrMakesTextVisibleAgain() {
        // Modes 1 and 2 draw visible (stroked) text; only 3 and 7 are invisible.
        String out = strip("BT\n3 Tr\nET\nBT\n/F1 10 Tf\n2 Tr\n(Heading) Tj\nET\n");
        assertTrue(out.contains("(Heading) Tj"), out);
    }

    @Test
    void laterClipOnlyTrStaysInvisible() {
        // Mode 7 adds to the clip path and draws nothing, so it is still hidden text.
        String out = strip("BT\n3 Tr\nET\nBT\n/F1 10 Tf\n7 Tr\n(hidden) Tj\nET\n");
        assertFalse(out.contains("(hidden)"), out);
    }
}
