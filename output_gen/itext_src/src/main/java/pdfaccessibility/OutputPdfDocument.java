// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import com.itextpdf.kernel.pdf.PdfConformance;
import com.itextpdf.kernel.pdf.PdfDictionary;
import com.itextpdf.kernel.pdf.PdfDocument;
import com.itextpdf.kernel.pdf.PdfName;
import com.itextpdf.kernel.pdf.PdfReader;
import com.itextpdf.kernel.pdf.PdfWriter;

/**
 * Document opened to produce output. iText reads the input's conformance on
 * open and writes it back to the XMP on close, so a PDF/UA claim made by the
 * input would survive any metadata edit. Output is not validated against
 * PDF/UA, so that claim is dropped here; PDF/A status is left as it was.
 *
 * The output is redistributed as an accessible copy of the input, so
 * document-level actions (OpenAction, catalog AA) and document JavaScript
 * from the source are dropped as well. Attachments are user content and are
 * kept.
 */
class OutputPdfDocument extends PdfDocument {

    OutputPdfDocument(PdfReader reader, PdfWriter writer) {
        super(reader, writer);
        pdfConformance = new PdfConformance(pdfConformance.getAConformance());
        stripDocumentActions(getCatalog().getPdfObject());
    }

    static void stripDocumentActions(PdfDictionary catalog) {
        catalog.remove(PdfName.OpenAction);
        catalog.remove(PdfName.AA);
        PdfDictionary names = catalog.getAsDictionary(PdfName.Names);
        if (names != null) {
            names.remove(PdfName.JavaScript);
            if (names.isEmpty()) {
                catalog.remove(PdfName.Names);
            } else {
                names.setModified();
            }
        }
        catalog.setModified();
    }
}
