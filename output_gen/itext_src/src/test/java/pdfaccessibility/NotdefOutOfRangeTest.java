// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.charset.StandardCharsets;
import java.util.Map;

import org.junit.jupiter.api.Test;

import com.itextpdf.kernel.pdf.PdfArray;
import com.itextpdf.kernel.pdf.PdfDictionary;
import com.itextpdf.kernel.pdf.PdfName;
import com.itextpdf.kernel.pdf.PdfNumber;

/**
 * Regression tests for the out-of-range .notdef rule, reproduced from a
 * real-world title font.
 *
 * A simple font may legally use a char code OUTSIDE its declared
 * /FirstChar../LastChar range: the code still renders when
 * /Encoding /Differences maps it to a real glyph present in the font
 * program (only the advance width falls back to /MissingWidth).
 * The font declares /LastChar 22 but its Differences
 * assigns code 23 = /y, and the page content uses code 23 ("military").
 * stripNotdefGlyphs must NOT delete that byte; genuinely dangling
 * out-of-range codes (no Differences name) must still be stripped
 * (veraPDF 7.21.8).
 */
class NotdefOutOfRangeTest {

    /** Mimics the title font: FirstChar 1, LastChar 22,
     *  22 widths, Differences assigning codes 1..23 (23 = /y). */
    private static PdfDictionary titleFontResources(boolean includeY) {
        PdfDictionary font = new PdfDictionary();
        font.put(PdfName.Subtype, PdfName.Type1);
        font.put(new PdfName("FirstChar"), new PdfNumber(1));
        font.put(new PdfName("LastChar"), new PdfNumber(22));
        PdfArray widths = new PdfArray();
        for (int i = 1; i <= 22; i++) widths.add(new PdfNumber(500));
        font.put(PdfName.Widths, widths);

        PdfDictionary enc = new PdfDictionary();
        enc.put(new PdfName("BaseEncoding"), new PdfName("WinAnsiEncoding"));
        PdfArray diffs = new PdfArray();
        diffs.add(new PdfNumber(1));
        String[] names = {"O", "r", "g", "a", "n", "i", "z", "t", "o", "l",
                          "space", "e", "h", "M", "C", "p", "s", "colon",
                          "c", "d", "m", "hyphen"};
        for (String g : names) diffs.add(new PdfName(g));
        if (includeY) diffs.add(new PdfName("y")); // code 23, outside range
        enc.put(new PdfName("Differences"), diffs);
        font.put(PdfName.Encoding, enc);

        PdfDictionary fonts = new PdfDictionary();
        fonts.put(new PdfName("T1_1"), font);
        PdfDictionary resources = new PdfDictionary();
        resources.put(PdfName.Font, fonts);
        return resources;
    }

    @Test
    void outOfRangeCodeWithDifferencesNameIsNotNotdef() {
        Map<String, PdfAccessibilityTagger.NotdefInfo> map =
            PdfAccessibilityTagger.findNotdefFonts(titleFontResources(true));
        PdfAccessibilityTagger.NotdefInfo info = map.get("T1_1");
        assertNotNull(info, "font with FirstChar>0 must be range-tracked");
        assertFalse(info.isNotdef(23),
            "code 23 is outside FirstChar..LastChar but /Differences maps"
            + " it to /y — it renders and must not be classified .notdef");
    }

    @Test
    void outOfRangeCodeWithoutDifferencesNameStaysNotdef() {
        Map<String, PdfAccessibilityTagger.NotdefInfo> map =
            PdfAccessibilityTagger.findNotdefFonts(titleFontResources(false));
        PdfAccessibilityTagger.NotdefInfo info = map.get("T1_1");
        assertNotNull(info);
        assertTrue(info.isNotdef(23),
            "out-of-range code with NO Differences mapping is a dangling"
            + " .notdef reference and must still be stripped (7.21.8)");
        assertTrue(info.isNotdef(0), "code 0 stays .notdef");
        assertTrue(info.isNotdef(200), "code 200 stays .notdef");
    }

    @Test
    void inRangeCodesUnaffected() {
        Map<String, PdfAccessibilityTagger.NotdefInfo> map =
            PdfAccessibilityTagger.findNotdefFonts(titleFontResources(true));
        PdfAccessibilityTagger.NotdefInfo info = map.get("T1_1");
        assertNotNull(info);
        for (int c = 1; c <= 22; c++) {
            assertFalse(info.isNotdef(c),
                "in-range named non-zero-width code " + c + " is real");
        }
    }

    @Test
    void stripNotdefGlyphsKeepsOctalEscapedOutOfRangeCode() {
        // Exact shape of the original page-2 H1 second line:
        // "military " = m i l i t a r y space
        //             = \025 \006 \n \006 \b \004 \002 \027 \013
        // where \027 (0x17, code 23) is the 'y'.
        String content = "BT\n/T1_1 14 Tf\n"
            + "[(\\025\\006\\n\\006\\b\\004\\002\\027\\013)] TJ\nET\n";
        byte[] out = PdfAccessibilityTagger.stripNotdefGlyphs(
            content.getBytes(StandardCharsets.ISO_8859_1),
            titleFontResources(true));
        String s = new String(out, StandardCharsets.ISO_8859_1);
        assertTrue(s.contains("\\027"),
            "octal escape \\027 (code 23 = /y) must survive stripping");
    }

    @Test
    void stripNotdefGlyphsStillDropsUnmappedOutOfRangeCode() {
        String content = "BT\n/T1_1 14 Tf\n"
            + "[(\\025\\006\\n\\006\\b\\004\\002\\027\\013)] TJ\nET\n";
        byte[] out = PdfAccessibilityTagger.stripNotdefGlyphs(
            content.getBytes(StandardCharsets.ISO_8859_1),
            titleFontResources(false));
        String s = new String(out, StandardCharsets.ISO_8859_1);
        assertFalse(s.contains("\\027"),
            "with no /Differences name, code 23 is dangling and must"
            + " still be stripped");
        assertTrue(s.contains("\\025"),
            "in-range codes on the same line must survive");
    }
}
