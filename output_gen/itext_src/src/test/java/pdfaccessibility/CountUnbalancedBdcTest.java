// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.nio.charset.StandardCharsets;

import org.junit.jupiter.api.Test;

/**
 * Unit tests for {@link PdfAccessibilityTagger#countUnbalancedBDC(byte[])}.
 * The counter drives EMC injection when inlining page content — a miscount
 * corrupts Artifact nesting, so BDC/BMC/EMC keywords inside painted strings
 * must not be counted.
 */
class CountUnbalancedBdcTest {

    private static int count(String content) {
        return PdfAccessibilityTagger.countUnbalancedBDC(
            content.getBytes(StandardCharsets.ISO_8859_1));
    }

    @Test
    void balancedContentCountsZero() {
        assertEquals(0, count("/P <</MCID 0>> BDC (x) Tj EMC"));
    }

    @Test
    void unbalancedBdcCountsOne() {
        assertEquals(1, count("/Span <</ActualText <FEFF0009>>> BDC (x) Tj"));
    }

    @Test
    void bdcInsidePaintedStringIsNotCounted() {
        assertEquals(0, count("BT (about BDC markers) Tj ET"));
    }

    @Test
    void emcInsidePaintedStringIsNotCounted() {
        // A real unbalanced BDC must still count even when a painted
        // string mentions EMC (which must NOT balance it).
        assertEquals(1,
            count("/P <</MCID 0>> BDC BT (see EMC spec) Tj ET"));
    }

    @Test
    void bdcInsideHexStringIsNotCounted() {
        assertEquals(0, count("BT <424443> Tj ET"));
    }
}
