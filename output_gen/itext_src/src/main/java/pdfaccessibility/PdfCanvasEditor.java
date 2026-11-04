// SPDX-License-Identifier: AGPL-3.0-or-later
/*
 * PdfCanvasEditor — Content stream editing framework for iText.
 *
 * Adapted from mkl-public/testarea-itext7 PdfCanvasEditor.java.
 * Extends PdfCanvasProcessor to create an editable copy of a page's
 * content stream. Override write() to modify, inject, or filter operators.
 *
 * Original: http://stackoverflow.com/questions/40951776
 * Author: mkl (original), adapted for iText 8+ and PDF accessibility.
 */
package pdfaccessibility;

import java.io.IOException;
import java.util.List;
import java.util.Set;

import com.itextpdf.kernel.exceptions.PdfException;
import com.itextpdf.kernel.pdf.*;
import com.itextpdf.kernel.pdf.canvas.PdfCanvas;
import com.itextpdf.kernel.pdf.canvas.parser.EventType;
import com.itextpdf.kernel.pdf.canvas.parser.PdfCanvasProcessor;
import com.itextpdf.kernel.pdf.canvas.parser.data.IEventData;
import com.itextpdf.kernel.pdf.canvas.parser.listener.IEventListener;

/**
 * A content stream editor that replays a page's content stream through
 * operator hooks, allowing injection of new operators (like BDC/EMC).
 * <p>
 * By default, creates an identical copy. Override {@link #write} to edit.
 */
public class PdfCanvasEditor extends PdfCanvasProcessor {

    /**
     * Edit the content stream of a specific page.
     * Replaces the page's /Contents with the edited stream.
     *
     * If usePageCanvas is true, creates the output canvas from the page
     * object so that openTag/closeTag properly register MCIDs in iText's
     * TagStructureContext (needed for correct ParentTree building).
     */
    public void editPage(PdfDocument pdfDocument, int pageNumber)
            throws IOException {
        editPage(pdfDocument, pageNumber, false);
    }

    public void editPage(PdfDocument pdfDocument, int pageNumber,
                          boolean usePageCanvas) throws IOException {
        if (pdfDocument.getReader() == null || pdfDocument.getWriter() == null) {
            throw new PdfException(
                "PdfDocument must be opened in stamping mode.");
        }

        PdfPage page = pdfDocument.getPage(pageNumber);
        byte[] originalContent = page.getContentBytes();
        PdfResources resources = page.getResources();

        PdfCanvas pdfCanvas;
        if (usePageCanvas) {
            // Create a fresh stream and replace the page's Contents.
            // Use the 3-arg PdfCanvas constructor (stream, resources, doc)
            // so it doesn't append to existing content.
            // The canvas still has access to the PdfDocument's
            // TagStructureContext for openTag/closeTag MCID registration.
            PdfStream newStream = new PdfStream();
            newStream.makeIndirect(pdfDocument);
            page.put(PdfName.Contents, newStream);
            pdfCanvas = new PdfCanvas(newStream, resources, pdfDocument);
        } else {
            pdfCanvas = new PdfCanvas(
                new PdfStream(), resources, pdfDocument);
        }

        editContent(originalContent, resources, pdfCanvas);

        if (!usePageCanvas) {
            page.put(PdfName.Contents, pdfCanvas.getContentStream());
        }
    }

    /**
     * Process content bytes and output edited content to the canvas.
     */
    public void editContent(byte[] contentBytes, PdfResources resources,
                             PdfCanvas canvas) {
        this.canvas = canvas;
        processContent(contentBytes, resources);
        this.canvas = null;
    }

    /**
     * Called before each operator is processed by its original handler.
     * Override to inspect state before the operator executes.
     */
    protected void nextOperation(PdfLiteral operator,
                                  List<PdfObject> operands) {
    }

    /**
     * Called to write each operator to the output stream.
     * Override to modify, inject, or filter operators.
     *
     * The default implementation writes the operator as-is.
     */
    protected void write(PdfCanvasProcessor processor,
                          PdfLiteral operator,
                          List<PdfObject> operands) {
        PdfOutputStream out =
            canvas.getContentStream().getOutputStream();
        int index = 0;
        for (PdfObject object : operands) {
            out.write(object);
            if (operands.size() > ++index)
                out.writeSpace();
            else
                out.writeNewLine();
        }
    }

    // --- Constructors ---

    public PdfCanvasEditor() {
        this(new DummyEventListener());
    }

    public PdfCanvasEditor(IEventListener listener) {
        super(listener);
    }

    // --- PdfCanvasProcessor override ---

    @Override
    public com.itextpdf.kernel.pdf.canvas.parser.IContentOperator
    registerContentOperator(
            String operatorString,
            com.itextpdf.kernel.pdf.canvas.parser.IContentOperator operator) {
        ContentOperatorWrapper wrapper = new ContentOperatorWrapper();
        wrapper.setOriginalOperator(operator);
        var former = super.registerContentOperator(operatorString, wrapper);
        return former instanceof ContentOperatorWrapper
            ? ((ContentOperatorWrapper) former).getOriginalOperator()
            : former;
    }

    // --- The output canvas ---

    protected PdfCanvas canvas = null;

    // --- Operator wrapper that hooks into write() ---

    class ContentOperatorWrapper
            implements com.itextpdf.kernel.pdf.canvas.parser.IContentOperator {

        private com.itextpdf.kernel.pdf.canvas.parser.IContentOperator
            originalOperator = null;

        public com.itextpdf.kernel.pdf.canvas.parser.IContentOperator
        getOriginalOperator() {
            return originalOperator;
        }

        public void setOriginalOperator(
                com.itextpdf.kernel.pdf.canvas.parser.IContentOperator op) {
            this.originalOperator = op;
        }

        @Override
        public void invoke(PdfCanvasProcessor processor,
                            PdfLiteral operator,
                            List<PdfObject> operands) {
            nextOperation(operator, operands);
            if (originalOperator != null
                    && !"Do".equals(operator.toString())) {
                try {
                    originalOperator.invoke(processor, operator, operands);
                } catch (RuntimeException e) {
                    // Malformed / under-specified content stream. Two known
                    // cases: a ClassCastException (e.g. PdfLiteral in a TJ array
                    // where a PdfNumber is expected), and a NullPointerException
                    // from iText resolving a named resource the page's
                    // /Resources doesn't declare (e.g. a "/CS0 CS" colorspace
                    // with no /ColorSpace entry). Either way, skip best-effort
                    // graphics-state tracking for this one operator and continue
                    // — it is still written verbatim to output via write()
                    // below, so visible content is unchanged. Non-fatal: one bad
                    // operator must not abort the entire tagging run.
                    System.err.println("WARNING: operator '" + operator
                        + "' failed in original handler: "
                        + e.getClass().getSimpleName() + ": " + e.getMessage());
                }
            }
            write(processor, operator, operands);
        }
    }

    // --- Dummy listener ---

    static class DummyEventListener implements IEventListener {
        @Override
        public void eventOccurred(IEventData data, EventType type) {}

        @Override
        public Set<EventType> getSupportedEvents() { return null; }
    }
}
