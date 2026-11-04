// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.charset.StandardCharsets;

import org.junit.jupiter.api.Test;

/**
 * Unit tests for {@link PdfAccessibilityTagger#stripMarkedContent(byte[])}.
 *
 * The strip pass removes stale BDC/BMC/EMC marked-content constructs from
 * source content streams. It must NEVER remove painting operators — an
 * un-stripped leftover is a validation flag at worst, a deleted painting
 * operator is silent content loss.
 */
class StripMarkedContentTest {

    private static String strip(String content) {
        byte[] out = PdfAccessibilityTagger.stripMarkedContent(
            content.getBytes(StandardCharsets.ISO_8859_1));
        return new String(out, StandardCharsets.ISO_8859_1);
    }

    // ------------------------------------------------------------------
    // Content preservation: over-match scenarios
    // ------------------------------------------------------------------

    @Test
    void orphanMcidDictAfterUnrelatedDictPreservesContentBetween() {
        String content = "/Foo <</A 1>> DP\n"
            + "q 1 0 0 1 10 20 cm\n"
            + "BT /F1 12 Tf (Hello world) Tj ET\n"
            + "Q\n"
            + "<</MCID 5>>\n"
            + "(more) Tj";
        String out = strip(content);
        assertTrue(out.contains("(Hello world) Tj"),
            "painted text between DP dict and orphan MCID dict must survive");
        assertTrue(out.contains("(more) Tj"));
        assertFalse(out.contains("/MCID 5"),
            "orphan MCID dict itself should be stripped");
    }

    @Test
    void bdcAfterUnrelatedDictPreservesContentBetween() {
        String content = "/Foo <</A 1>> DP\n"
            + "BT (visible text) Tj ET\n"
            + "/Span <</MCID 2>> BDC (x) Tj EMC";
        String out = strip(content);
        assertTrue(out.contains("(visible text) Tj"),
            "painted text between DP dict and BDC must survive");
        assertFalse(out.contains("BDC"));
        assertFalse(out.contains("/MCID 2"));
    }

    @Test
    void emcInsidePaintedStringIsNotStripped() {
        String content = "BT (See EMC standard) Tj ET EMC";
        String out = strip(content);
        assertTrue(out.contains("(See EMC standard) Tj"),
            "the bytes 'EMC' inside a painted literal string must survive");
        assertFalse(out.matches("(?s).*\\)\\s*Tj\\s*ET\\s*EMC.*"),
            "the real EMC operator after ET must be stripped");
    }

    @Test
    void bdcAndBmcInsidePaintedStringAreNotStripped() {
        String content = "BT (about BDC and BMC ops) Tj ET";
        String out = strip(content);
        assertEquals(content, out,
            "BDC/BMC bytes inside painted strings must survive untouched");
    }

    // ------------------------------------------------------------------
    // Legitimate strips must keep working
    // ------------------------------------------------------------------

    @Test
    void bdcWithActualTextHexStringIsStripped() {
        // Documented case: hex string inside the property dict
        String content = "/Span <</MCID 5 /ActualText <FEFF0048>>> BDC (H) Tj EMC";
        String out = strip(content);
        assertFalse(out.contains("BDC"));
        assertFalse(out.contains("/MCID"));
        assertFalse(out.contains("EMC"));
        assertTrue(out.contains("(H) Tj"));
    }

    @Test
    void simpleBdcIsStripped() {
        String out = strip("/P <</MCID 3>> BDC (t) Tj EMC");
        assertFalse(out.contains("BDC"));
        assertTrue(out.contains("(t) Tj"));
    }

    @Test
    void multilineBdcWithLiteralStringPropIsStripped() {
        String content = "/P\n<< /MCID 12\n/Lang (en-US) >>\nBDC\n(t) Tj\nEMC";
        String out = strip(content);
        assertFalse(out.contains("BDC"));
        assertFalse(out.contains("/MCID 12"));
        assertTrue(out.contains("(t) Tj"));
    }

    @Test
    void bdcWithNestedParensInAltStringIsStripped() {
        String content = "/Figure <</MCID 0 /Alt (a (nested) pic)>> BDC re f EMC";
        String out = strip(content);
        assertFalse(out.contains("BDC"));
        assertTrue(out.contains("re f"));
    }

    @Test
    void bdcWithEscapedParenInStringPropIsStripped() {
        String content = "/P <</MCID 1 /Alt (open \\( only)>> BDC (t) Tj EMC";
        String out = strip(content);
        assertFalse(out.contains("BDC"));
        assertTrue(out.contains("(t) Tj"));
    }

    @Test
    void namedPropertiesBdcIsStripped() {
        String out = strip("/OC /MC0 BDC (t) Tj EMC\n/Suspect/R14 BDC (u) Tj EMC");
        assertFalse(out.contains("BDC"));
        assertTrue(out.contains("(t) Tj"));
        assertTrue(out.contains("(u) Tj"));
    }

    @Test
    void bmcIsStripped() {
        String out = strip("/Artifact BMC (t) Tj EMC");
        assertFalse(out.contains("BMC"));
        assertTrue(out.contains("(t) Tj"));
    }

    @Test
    void orphanMcidDictVariantsAreStripped() {
        assertFalse(strip("<</MCID 5>>").contains("/MCID"));
        assertFalse(strip("<</MCID 5 /ActualText <FEFF0041>>>").contains("/MCID"));
        assertFalse(strip("<< /MCID 7 /Lang (en) >>").contains("/MCID"));
    }

    @Test
    void contentWithoutMarkedContentIsUnchanged() {
        String content = "q\nBT /F1 10 Tf 72 700 Td (plain page) Tj ET\nQ\n"
            + "0.5 w 10 10 100 100 re S\n";
        assertEquals(content, strip(content));
    }
}
