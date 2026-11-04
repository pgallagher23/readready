// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.io.ByteArrayOutputStream;
import java.util.List;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

import com.itextpdf.kernel.geom.Rectangle;
import com.itextpdf.kernel.pdf.PdfDictionary;
import com.itextpdf.kernel.pdf.PdfDocument;
import com.itextpdf.kernel.pdf.PdfLiteral;
import com.itextpdf.kernel.pdf.PdfName;
import com.itextpdf.kernel.pdf.PdfNumber;
import com.itextpdf.kernel.pdf.PdfObject;
import com.itextpdf.kernel.pdf.PdfPage;
import com.itextpdf.kernel.pdf.PdfStream;
import com.itextpdf.kernel.pdf.PdfWriter;
import com.itextpdf.kernel.pdf.xobject.PdfFormXObject;

/**
 * Unit tests for {@link ContentStreamAnalyzer#classifyDo}.
 *
 * The analyzer and the content-preserving writer MUST classify every Do
 * operator identically — a disagreement silently shifts the form/image
 * tag-reference indices for the rest of the page (wrong alt text on
 * wrong figures). This shared classifier is the single source of truth.
 */
class ClassifyDoTest {

    private PdfDocument doc;
    private PdfPage page;

    @BeforeEach
    void setUp() {
        doc = new PdfDocument(new PdfWriter(new ByteArrayOutputStream()));
        page = doc.addNewPage();

        PdfDictionary xobjs = new PdfDictionary();

        PdfFormXObject form = new PdfFormXObject(new Rectangle(0, 0, 10, 10));
        form.makeIndirect(doc);
        xobjs.put(new PdfName("Fm1"), form.getPdfObject());

        PdfStream img = new PdfStream(new byte[] {(byte) 0xFF});
        img.put(PdfName.Type, PdfName.XObject);
        img.put(PdfName.Subtype, PdfName.Image);
        img.put(PdfName.Width, new PdfNumber(1));
        img.put(PdfName.Height, new PdfNumber(1));
        img.put(PdfName.ColorSpace, PdfName.DeviceGray);
        img.put(PdfName.BitsPerComponent, new PdfNumber(8));
        img.makeIndirect(doc);
        xobjs.put(new PdfName("Im1"), img);

        // Non-form, non-image subtype (e.g. legacy PostScript XObject)
        PdfStream ps = new PdfStream(new byte[] {0x20});
        ps.put(PdfName.Type, PdfName.XObject);
        ps.put(PdfName.Subtype, new PdfName("PS"));
        ps.makeIndirect(doc);
        xobjs.put(new PdfName("Ps1"), ps);

        page.getResources().getPdfObject().put(PdfName.XObject, xobjs);
    }

    @AfterEach
    void tearDown() {
        doc.close();
    }

    private static List<PdfObject> doOperands(PdfObject name) {
        return List.of(name, new PdfLiteral("Do"));
    }

    @Test
    void formXObjectClassifiesAsForm() {
        assertEquals(ContentStreamAnalyzer.XObjectKind.FORM,
            ContentStreamAnalyzer.classifyDo(page,
                doOperands(new PdfName("Fm1"))));
    }

    @Test
    void imageXObjectClassifiesAsImage() {
        assertEquals(ContentStreamAnalyzer.XObjectKind.IMAGE,
            ContentStreamAnalyzer.classifyDo(page,
                doOperands(new PdfName("Im1"))));
    }

    @Test
    void missingXObjectClassifiesAsOther() {
        assertEquals(ContentStreamAnalyzer.XObjectKind.OTHER,
            ContentStreamAnalyzer.classifyDo(page,
                doOperands(new PdfName("NoSuch"))));
    }

    @Test
    void nonImageNonFormSubtypeClassifiesAsOther() {
        assertEquals(ContentStreamAnalyzer.XObjectKind.OTHER,
            ContentStreamAnalyzer.classifyDo(page,
                doOperands(new PdfName("Ps1"))));
    }

    @Test
    void wrongOperandTypeClassifiesAsOther() {
        assertEquals(ContentStreamAnalyzer.XObjectKind.OTHER,
            ContentStreamAnalyzer.classifyDo(page,
                doOperands(new PdfNumber(3))));
    }

    @Test
    void emptyOperandsClassifiesAsOther() {
        assertEquals(ContentStreamAnalyzer.XObjectKind.OTHER,
            ContentStreamAnalyzer.classifyDo(page, List.of()));
    }

    @Test
    void pageWithoutXObjectResourcesClassifiesAsOther() {
        PdfPage bare = doc.addNewPage();
        assertEquals(ContentStreamAnalyzer.XObjectKind.OTHER,
            ContentStreamAnalyzer.classifyDo(bare,
                doOperands(new PdfName("Fm1"))));
    }
}
