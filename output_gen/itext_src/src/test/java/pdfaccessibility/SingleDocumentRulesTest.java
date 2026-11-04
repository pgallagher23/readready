// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import org.junit.jupiter.api.Test;

/**
 * Rules that once matched one document's quirks too loosely: they must fire
 * on the real pattern and leave ordinary content alone.
 */
class SingleDocumentRulesTest {

    @Test
    void privateUseCheckLooksAtTargetsNotSourceCodes() {
        // Byte 0xE0 mapped to U+00E0 ('à') is a normal mapping.
        assertFalse(PdfAccessibilityTagger.cmapMapsToPrivateUse(
            "1 beginbfchar\n<E0> <00E0>\nendbfchar"));
        assertTrue(PdfAccessibilityTagger.cmapMapsToPrivateUse(
            "1 beginbfchar\n<01> <E001>\nendbfchar"));
        assertTrue(PdfAccessibilityTagger.cmapMapsToPrivateUse(
            "1 beginbfrange\n<20> <7E> <E020>\nendbfrange"));
        assertTrue(PdfAccessibilityTagger.cmapMapsToPrivateUse(
            "1 beginbfrange\n<01> <02> [<0041> <F702>]\nendbfrange"));
        // codespacerange entries are not targets.
        assertFalse(PdfAccessibilityTagger.cmapMapsToPrivateUse(
            "1 begincodespacerange\n<E000> <F8FF>\nendcodespacerange\n"
            + "1 beginbfchar\n<41> <0041>\nendbfchar"));
    }

    @Test
    void placeholderTestOnlyMatchesUnderscoreInsideBrackets() {
        assertFalse(ContentPreservingTagger.isUsableText("Sign here: [Name_here]"));
        assertTrue(ContentPreservingTagger.isUsableText("see [3] for snake_case names"));
        assertTrue(ContentPreservingTagger.isUsableText("x_i = [a, b]"));
    }

    @Test
    void boldTextStartingWithLevelGetsNoSpecialHeadingLevel() {
        assertEquals("H3", PdfAccessibilityTagger.boldHeadingTag(11.0, "Level 2 Requirements"));
        assertEquals("H3", PdfAccessibilityTagger.boldHeadingTag(11.0, "Overview"));
        assertEquals("H1", PdfAccessibilityTagger.boldHeadingTag(16.0, "Level 2 Requirements"));
    }
}
