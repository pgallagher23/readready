// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;

import org.junit.jupiter.api.Test;

import com.itextpdf.kernel.pdf.PdfDocument;
import com.itextpdf.kernel.pdf.PdfReader;
import com.itextpdf.kernel.pdf.PdfWriter;
import com.itextpdf.kernel.xmp.XMPConst;
import com.itextpdf.kernel.xmp.XMPMeta;
import com.itextpdf.kernel.xmp.XMPMetaFactory;

/** Output must not declare a conformance level, even if the input PDF did. */
class XmpIdentifierTest {

    private static final String PDFUA_NS = "http://www.aiim.org/pdfua/ns/id/";

    private static final String PDFA_NS = "http://www.aiim.org/pdfa/ns/id/";

    /** Writes a one-page PDF whose XMP carries the given identifiers. */
    private static byte[] input(boolean pdfUa, boolean pdfA) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (PdfDocument pdf = new PdfDocument(new PdfWriter(out))) {
            pdf.addNewPage();
            XMPMeta xmp = XMPMetaFactory.create();
            if (pdfUa) {
                XMPMetaFactory.getSchemaRegistry().registerNamespace(PDFUA_NS, "pdfuaid");
                xmp.setPropertyInteger(PDFUA_NS, "part", 1);
            }
            if (pdfA) {
                XMPMetaFactory.getSchemaRegistry().registerNamespace(PDFA_NS, "pdfaid");
                xmp.setPropertyInteger(PDFA_NS, "part", 2);
                xmp.setProperty(PDFA_NS, "conformance", "B");
            }
            pdf.setXmpMetadata(xmp);
        }
        return out.toByteArray();
    }

    /** Stamps the input the way the taggers do, then reads the saved XMP back. */
    private static XMPMeta stamp(byte[] input) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (PdfDocument pdf = new OutputPdfDocument(
                new PdfReader(new ByteArrayInputStream(input)), new PdfWriter(out))) {
            PdfAccessibilityTagger.setXmpMetadata(pdf, "Title", "en");
        }
        try (PdfDocument read = new PdfDocument(
                new PdfReader(new ByteArrayInputStream(out.toByteArray())))) {
            return XMPMetaFactory.parseFromBuffer(read.getXmpMetadataBytes());
        }
    }

    @Test
    void identifierIsNotAdded() throws Exception {
        XMPMeta xmp = stamp(input(false, false));
        assertFalse(xmp.doesPropertyExist(PDFUA_NS, "part"));
        assertEquals("Title", xmp.getLocalizedText(XMPConst.NS_DC, "title", "x-default", "x-default").getValue());
    }

    @Test
    void identifierFromInputIsRemoved() throws Exception {
        assertFalse(stamp(input(true, false)).doesPropertyExist(PDFUA_NS, "part"));
    }

    @Test
    void pdfAStatusIsKept() throws Exception {
        XMPMeta xmp = stamp(input(false, true));
        assertEquals(Integer.valueOf(2), xmp.getPropertyInteger(PDFA_NS, "part"));
        assertFalse(xmp.doesPropertyExist(PDFUA_NS, "part"));
    }
}
