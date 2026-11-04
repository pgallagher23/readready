// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;

import org.junit.jupiter.api.Test;

import com.itextpdf.kernel.font.PdfFont;
import com.itextpdf.kernel.geom.PageSize;
import com.itextpdf.kernel.pdf.PdfDocument;
import com.itextpdf.kernel.pdf.PdfPage;
import com.itextpdf.kernel.pdf.PdfReader;
import com.itextpdf.kernel.pdf.PdfWriter;
import com.itextpdf.kernel.pdf.canvas.PdfCanvas;
import com.itextpdf.kernel.pdf.canvas.parser.PdfTextExtractor;
import com.itextpdf.kernel.pdf.tagging.StandardRoles;
import com.itextpdf.kernel.pdf.tagutils.TagTreePointer;

/**
 * Regression tests for {@link PdfAccessibilityTagger#writeDistributedMCR}
 * on rotated overlay (scanned) pages.
 *
 * <p>Root cause fixed here: on a rotated page every paragraph flows through
 * the overlay path, and Docling's per-element bbox is frequently a degenerate
 * sliver (e.g. 6pt wide).  The word-distribution layout then wraps after every
 * word and marches off the page, so the tail of a long paragraph was either
 * dropped by the bbox-bottom {@code break} or emitted at off-page coordinates —
 * silent screen-reader content loss.  A bbox that does not fit the text at
 * any usable font size — degenerate on any rotation, not just rotated pages —
 * now falls back to a single showText for the whole string, sized to fit the
 * page.
 *
 * <p>The fix was later generalized (fit-to-bbox layout) so it is no longer
 * gated on rotation: a degenerate bbox falls back to the single-showText path
 * on any rotation, and a proper bbox is laid out at a font size guaranteed to
 * fit, so nothing truncates on rotated or non-rotated pages.
 */
class WriteDistributedMcrRotationTest {

    // ~85 words; the reported page-3 paragraph that was being truncated.
    private static final String PARAGRAPH =
        "available on a college campus that are relevant and were not provided "
        + "here What are the legal precedents for actions that may be taken To "
        + "whom might you need to talk to gather further information Remember "
        + "that decision makers seldom have all of the information they want or "
        + "need and often they must make reasonable guesses or assumptions about "
        + "certain key facts This step reminds the analyst to be explicit about "
        + "his or her assumptions";

    // Same degenerate sliver bbox Docling emits for this paragraph on the
    // rotated scanned page (6.33pt wide x 58pt tall).
    private static final float BL = 37f, BB = 38.67f, BR = 43.33f, BT = 96.67f;

    /**
     * Tag the paragraph as a P element at the given rotation, flush to bytes,
     * reopen (so the embedded font subset is materialized), and return the
     * page text decoded via ToUnicode (invisible render mode is irrelevant to
     * extraction).
     */
    private String writeAndExtract(int rotation, float mbW, float mbH,
            float pageW, float pageH) throws Exception {
        return writeAndExtractBbox(rotation, mbW, mbH, pageW, pageH, BL, BB, BR, BT);
    }

    private String writeAndExtractBbox(int rotation, float mbW, float mbH,
            float pageW, float pageH,
            float bl, float bb, float br, float bt) throws Exception {
        ByteArrayOutputStream baos = new ByteArrayOutputStream();
        try (PdfDocument doc = new PdfDocument(new PdfWriter(baos))) {
            doc.setTagged();
            PdfFont font = PdfAccessibilityTagger.loadEmbeddedFont();
            // The fix is about embedded-font layout; if no TrueType font is
            // installed the fallback is non-embedded Helvetica with different
            // metrics — skip rather than assert on an unrepresentative env.
            assumeTrue(font != null && font.getFontProgram() != null
                    && font.isEmbedded(),
                "no embeddable TrueType font available on this system");
            PdfPage page = doc.addNewPage(new PageSize(pageW, pageH));
            PdfCanvas canvas = new PdfCanvas(page);
            TagTreePointer tp = new TagTreePointer(doc);
            tp.setPageForTagging(page);
            tp.addTag(StandardRoles.P);
            PdfAccessibilityTagger.writeDistributedMCR(
                canvas, tp, font, PARAGRAPH, bl, bb, br, bt,
                rotation, mbW, mbH);
        }
        try (PdfDocument reopened = new PdfDocument(
                new PdfReader(new ByteArrayInputStream(baos.toByteArray())))) {
            return PdfTextExtractor.getTextFromPage(reopened.getPage(1))
                .replaceAll("\\s+", " ");
        }
    }

    /**
     * On a 270-rotated page (landscape MediaBox displayed portrait) with a
     * degenerate bbox, the whole paragraph must survive — including its tail,
     * which the old layout dropped off-page.
     */
    @Test
    void rotated270DegenerateBboxKeepsWholeParagraph() throws Exception {
        String text = writeAndExtract(270, 792f, 612f, 792f, 612f);
        assertTrue(text.contains("available on a college campus"),
            "paragraph head must be present; got: " + text);
        assertTrue(text.contains("legal precedents"),
            "paragraph middle must be present; got: " + text);
        assertTrue(text.contains("explicit about his or her assumptions"),
            "paragraph TAIL must be present (was previously truncated/off-page); "
            + "got: " + text);
    }

    /**
     * A non-rotated degenerate/sliver bbox can't fit the paragraph at any
     * usable font size either, so the single-showText fallback — ungated by
     * rotation — preserves the whole paragraph here too, just as it does for
     * the rotated case above.
     */
    @Test
    void nonRotatedDegenerateBboxKeepsWholeParagraph() throws Exception {
        // Same degenerate sliver, rotation 0: fit-to-bbox can't fit it either,
        // so the single-showText fallback preserves the whole paragraph.
        String text = writeAndExtract(0, 612f, 792f, 612f, 792f);
        assertTrue(text.contains("available on a college campus"),
            "head must be present; got: " + text);
        assertTrue(text.contains("explicit about his or her assumptions"),
            "TAIL must be present on non-rotated degenerate bbox too; got: " + text);
    }

    @Test
    void nonRotatedProperBboxKeepsWholeParagraph() throws Exception {
        // Wide, short body-paragraph bbox (like Docling emits on normal scans):
        // the fit-to-bbox path distributes words and drops nothing.
        String text = writeAndExtractBbox(
            0, 612f, 792f, 612f, 792f,
            66f, 385f, 533f, 429f);   // 467x44pt
        assertTrue(text.contains("available on a college campus"),
            "head must be present; got: " + text);
        assertTrue(text.contains("legal precedents"),
            "middle must be present; got: " + text);
        assertTrue(text.contains("explicit about his or her assumptions"),
            "TAIL must be present (previously truncated on proper bbox); got: " + text);
    }

    /**
     * Rotated page, proper (non-degenerate) bbox: before the fit-to-bbox
     * generalization, {@code rotation != 0} always took the single-showText
     * fallback, so the per-line distribute path never ran under rotation.
     * This bbox is generous enough that {@code computeFitFontSize} finds a
     * font that fits, so the normal per-line distribute loop runs here
     * instead — the whole paragraph, including its tail, must still be
     * present.
     */
    @Test
    void rotated270ProperBboxKeepsWholeParagraph() throws Exception {
        // Wide, tall display-space box on the same landscape mediabox/page as
        // the degenerate-rotated case above, but large enough (510x360) that
        // computeFitFontSize fits a real font size (~26pt here) and the
        // per-line distribute path runs rather than the single-showText
        // fallback.
        String text = writeAndExtractBbox(
            270, 792f, 612f, 792f, 612f,
            50f, 200f, 560f, 560f);   // 510x360
        assertTrue(text.contains("available on a college campus"),
            "head must be present; got: " + text);
        assertTrue(text.contains("legal precedents"),
            "middle must be present; got: " + text);
        assertTrue(text.contains("explicit about his or her assumptions"),
            "TAIL must be present (distribute path, rotated); got: " + text);
    }
}
