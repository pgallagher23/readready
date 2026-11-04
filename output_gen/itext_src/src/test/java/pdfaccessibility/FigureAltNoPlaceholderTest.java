// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

import org.junit.jupiter.api.Test;

import pdfaccessibility.ContentPreservingTagger.ElementInfo;

/**
 * A Figure with no real alt text must get NO /Alt at all.
 *
 * The tagger used to write the literal string "Figure" (and "Formula") when
 * alt text was unavailable. veraPDF, PAC and Ally all accept any non-empty
 * /Alt, so such a document scored as compliant while a screen reader read
 * "Figure, Figure, Figure". Omitting /Alt fails PDF/UA 7.3 honestly instead.
 *
 * "Formula: " + text is NOT a placeholder — it is derived from the decoded
 * formula content — so it stays.
 */
public class FigureAltNoPlaceholderTest {

    private static ElementInfo figure(String altText) {
        return new ElementInfo("elem_0001", "Figure", null, altText, 1);
    }

    private static ElementInfo formula(String text, String altText) {
        return new ElementInfo("elem_0002", "Formula", text, altText, 1);
    }

    // --- Figure ---------------------------------------------------------

    @Test
    void nullAltYieldsNoAlt() {
        assertNull(ContentPreservingTagger.resolveFigureAlt(figure(null)));
    }

    @Test
    void emptyAltYieldsNoAlt() {
        assertNull(ContentPreservingTagger.resolveFigureAlt(figure("")));
    }

    @Test
    void whitespaceOnlyAltYieldsNoAlt() {
        assertNull(ContentPreservingTagger.resolveFigureAlt(figure("   ")));
    }

    @Test
    void realAltPassesThroughVerbatim() {
        String authored = "A screenshot of a Twitter post from Michael Duffy.";
        assertEquals(authored,
            ContentPreservingTagger.resolveFigureAlt(figure(authored)));
    }

    @Test
    void neverEmitsTheFigurePlaceholder() {
        for (String noAlt : new String[] {null, "", "   ", "\t\n"}) {
            assertNotEquals("Figure",
                ContentPreservingTagger.resolveFigureAlt(figure(noAlt)),
                "the literal \"Figure\" placeholder must never be emitted");
        }
    }

    // --- Formula --------------------------------------------------------

    @Test
    void formulaWithoutAltUsesDecodedText() {
        assertEquals("Formula: E = mc^2",
            ContentPreservingTagger.resolveFormulaAlt(formula("E = mc^2", null)));
    }

    @Test
    void formulaAltWinsOverDecodedText() {
        assertEquals("Einstein's mass-energy equivalence.",
            ContentPreservingTagger.resolveFormulaAlt(
                formula("E = mc^2", "Einstein's mass-energy equivalence.")));
    }

    @Test
    void formulaWithNeitherAltNorTextYieldsNoAlt() {
        assertNull(ContentPreservingTagger.resolveFormulaAlt(formula(null, null)));
    }

    @Test
    void neverEmitsTheFormulaPlaceholder() {
        assertNotEquals("Formula",
            ContentPreservingTagger.resolveFormulaAlt(formula(null, null)));
        assertNotEquals("Formula",
            ContentPreservingTagger.resolveFormulaAlt(formula("   ", null)));
    }
}
