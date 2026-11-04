// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;

import org.junit.jupiter.api.Test;

import com.itextpdf.kernel.pdf.PdfDictionary;
import com.itextpdf.kernel.pdf.PdfDocument;
import com.itextpdf.kernel.pdf.PdfName;
import com.itextpdf.kernel.pdf.PdfReader;
import com.itextpdf.kernel.pdf.PdfString;
import com.itextpdf.kernel.pdf.PdfWriter;
import com.itextpdf.kernel.pdf.action.PdfAction;

/**
 * The remediated output is redistributed as an accessible copy of the input.
 * Document-level actions and scripts from the source must not ride along.
 */
class CatalogActionStripTest {

    private static byte[] input() throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (PdfDocument pdf = new PdfDocument(new PdfWriter(out))) {
            pdf.addNewPage();
            pdf.getCatalog().setOpenAction(PdfAction.createJavaScript("app.alert(1)"));
            pdf.getCatalog().setAdditionalAction(PdfName.WC,
                PdfAction.createJavaScript("app.alert(2)"));
            pdf.getCatalog().getNameTree(PdfName.JavaScript)
                .addEntry("init", PdfAction.createJavaScript("app.alert(3)").getPdfObject());
            pdf.addFileAttachment("data.txt",
                com.itextpdf.kernel.pdf.filespec.PdfFileSpec.createEmbeddedFileSpec(
                    pdf, "hello".getBytes(), "data.txt", "data.txt", null, null));
        }
        return out.toByteArray();
    }

    private static PdfDictionary stampedCatalog() throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (PdfDocument pdf = new OutputPdfDocument(
                new PdfReader(new ByteArrayInputStream(input())), new PdfWriter(out))) {
            PdfAccessibilityTagger.setXmpMetadata(pdf, "Title", "en");
        }
        PdfDocument read = new PdfDocument(new PdfReader(new ByteArrayInputStream(out.toByteArray())));
        return read.getCatalog().getPdfObject();
    }

    @Test
    void openActionAndDocumentActionsAreRemoved() throws Exception {
        PdfDictionary cat = stampedCatalog();
        assertNull(cat.get(PdfName.OpenAction));
        assertNull(cat.get(PdfName.AA));
    }

    @Test
    void documentJavaScriptIsRemoved() throws Exception {
        PdfDictionary names = stampedCatalog().getAsDictionary(PdfName.Names);
        assertFalse(names != null && names.containsKey(PdfName.JavaScript));
    }

    @Test
    void attachmentsAreKept() throws Exception {
        PdfDictionary names = stampedCatalog().getAsDictionary(PdfName.Names);
        assertNotNull(names);
        assertNotNull(names.get(PdfName.EmbeddedFiles));
    }
}
