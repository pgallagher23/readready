// SPDX-License-Identifier: AGPL-3.0-or-later
/*
 * Content Stream Analyzer — Phase 1 of Content-Preserving Tagger
 *
 * Read-only analysis of PDF content streams using iText's PdfCanvasProcessor.
 * Extracts positioned text runs, images, and paths from each page, outputting
 * JSON that the Python matching layer (Phase 2) uses to map Docling structure
 * elements to actual content stream operators.
 *
 * This class does NOT modify the PDF in any way.
 *
 * Usage:
 *   java -jar itext_tagger.jar --analyze --input file.pdf --output analysis.json
 */
package pdfaccessibility;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.itextpdf.kernel.geom.LineSegment;
import com.itextpdf.kernel.geom.Matrix;
import com.itextpdf.kernel.geom.Rectangle;
import com.itextpdf.kernel.geom.Vector;
import com.itextpdf.kernel.pdf.*;
import com.itextpdf.kernel.pdf.canvas.parser.EventType;
import com.itextpdf.kernel.pdf.canvas.parser.PdfCanvasProcessor;
import com.itextpdf.kernel.pdf.canvas.parser.data.IEventData;
import com.itextpdf.kernel.pdf.canvas.parser.data.ImageRenderInfo;
import com.itextpdf.kernel.pdf.canvas.parser.data.TextRenderInfo;
import com.itextpdf.kernel.pdf.canvas.parser.IContentOperator;
import com.itextpdf.kernel.pdf.canvas.parser.listener.IEventListener;

import java.io.FileWriter;
import java.nio.charset.StandardCharsets;
import java.util.*;

public class ContentStreamAnalyzer {

    /**
     * How a Do operator's target is classified.  OTHER covers everything
     * that produces no image event and no form invocation record: a
     * missing XObject, a malformed operand, or a non-image non-form
     * subtype (e.g. legacy PostScript XObjects).
     */
    public enum XObjectKind { FORM, IMAGE, OTHER }

    /**
     * Classify a Do operator's target XObject.
     *
     * This is the SINGLE source of truth shared by this analyzer and the
     * content-preserving writer (ContentPreservingTagger).  The two passes
     * maintain parallel per-page form/image index counters; if they ever
     * classified a Do differently, every subsequent form/image tag
     * reference on the page would silently shift by one (wrong alt text
     * on wrong figures).  Never classify here in one class and inline in
     * the other.
     */
    public static XObjectKind classifyDo(PdfPage page,
            List<PdfObject> operands) {
        try {
            if (operands == null || operands.isEmpty()
                    || !(operands.get(0) instanceof PdfName)) {
                return XObjectKind.OTHER;
            }
            PdfName name = (PdfName) operands.get(0);
            PdfResources resources = page.getResources();
            if (resources == null) return XObjectKind.OTHER;
            PdfDictionary xobjs =
                resources.getPdfObject().getAsDictionary(PdfName.XObject);
            if (xobjs == null) return XObjectKind.OTHER;
            PdfStream xobjStream = xobjs.getAsStream(name);
            if (xobjStream == null) return XObjectKind.OTHER;
            PdfName subtype = xobjStream.getAsName(PdfName.Subtype);
            if (PdfName.Form.equals(subtype)) return XObjectKind.FORM;
            if (PdfName.Image.equals(subtype)) return XObjectKind.IMAGE;
            return XObjectKind.OTHER;
        } catch (Exception e) {
            System.err.println("WARNING: Do operand classification failed ("
                + e.getClass().getSimpleName() + ") — treating as OTHER");
            return XObjectKind.OTHER;
        }
    }

    /**
     * Analyze all pages of a PDF and return structured JSON.
     */
    public static String analyze(String inputPath) throws Exception {
        PdfReader reader = new PdfReader(inputPath);
        PdfDocument pdfDoc = new PdfDocument(reader);
        int numPages = pdfDoc.getNumberOfPages();

        Map<String, Object> result = new LinkedHashMap<>();
        result.put("file", inputPath);
        result.put("pages", numPages);

        List<Map<String, Object>> pageAnalyses = new ArrayList<>();

        for (int p = 1; p <= numPages; p++) {
            PdfPage page = pdfDoc.getPage(p);
            PageAnalysisListener listener = new PageAnalysisListener();

            // CropBox normalization: when CropBox differs from MediaBox,
            // content stream coordinates are in MediaBox space but Docling
            // reports in CropBox-relative space.  Compute the offset and
            // pass it to the listener so all coordinates are normalized.
            Rectangle mediaBox = page.getMediaBox();
            Rectangle cropBox = page.getCropBox();  // returns MediaBox if absent
            float cropOffsetX = 0, cropOffsetY = 0;
            float reportWidth, reportHeight;
            boolean hasCropBox = false;
            if (cropBox != null && !cropBox.equalsWithEpsilon(mediaBox)) {
                cropOffsetX = cropBox.getLeft() - mediaBox.getLeft();
                cropOffsetY = cropBox.getBottom() - mediaBox.getBottom();
                reportWidth = cropBox.getWidth();
                reportHeight = cropBox.getHeight();
                hasCropBox = true;
            } else {
                reportWidth = mediaBox.getWidth();
                reportHeight = mediaBox.getHeight();
            }

            // Fallback: detect non-standard MediaBox origin when no explicit
            // CropBox.  PDFs like [0 0 612 -792] have content stream coords
            // in negative Y space (bottom=-792, top=0).  PDFs like
            // [0 3.6 612 795.6] have coords shifted by a small positive
            // offset.  In both cases Docling reports bboxes in visual/CropBox
            // space (0-based).  Normalize by subtracting the MediaBox origin.
            if (!hasCropBox) {
                float mbLeft = mediaBox.getLeft();
                float mbBottom = mediaBox.getBottom();
                if (Math.abs(mbLeft) > 0.5f || Math.abs(mbBottom) > 0.5f) {
                    cropOffsetX = mbLeft;
                    cropOffsetY = mbBottom;
                    hasCropBox = true;
                    System.err.println("  Page " + p
                        + ": non-standard MediaBox origin ("
                        + round(mbLeft) + ", " + round(mbBottom)
                        + "), applying offset normalization");
                }
            }

            listener.cropOffsetX = cropOffsetX;
            listener.cropOffsetY = cropOffsetY;

            PdfCanvasProcessor processor = new PdfCanvasProcessor(listener);

            // Operator-level run indexing: wrap Tj/TJ/'/" handlers so that
            // all RENDER_TEXT events from a single TJ array are aggregated
            // into one run.  iText fires one event per TJ segment (e.g.,
            // [(H)-1(e)-2(l)]TJ fires 3 events), but the editor counts
            // one textRunIndex per operator.  Without aggregation the
            // analyzer produces N runs per operator while the editor
            // increments by 1, causing fatal index misalignment.
            for (String textOp : new String[]{"Tj", "TJ", "'", "\""}) {
                final IContentOperator[] origHolder = new IContentOperator[1];
                origHolder[0] = processor.registerContentOperator(textOp,
                    new IContentOperator() {
                        @Override
                        public void invoke(PdfCanvasProcessor proc,
                                PdfLiteral op, List<PdfObject> operands) {
                            listener.beginTextOperator();
                            if (origHolder[0] != null)
                                origHolder[0].invoke(proc, op, operands);
                            listener.endTextOperator();
                        }
                    });
            }

            // Skip Form XObject recursion: text inside Form XObjects is
            // invisible to ContentPreservingEditor (which skips the Do
            // operator's original handler).  Counting Form XObject text
            // runs inflates analyzer indices beyond what the editor sees.
            // However, record Form XObject invocations with their page-space
            // bounding boxes so the matcher can map unmatched Docling
            // elements to XObject content (veraPDF 7.20 t2 compliance).
            final PdfPage currentPage = page;
            final float fCropOffsetX = cropOffsetX;
            final float fCropOffsetY = cropOffsetY;
            final IContentOperator[] origDo = new IContentOperator[1];
            origDo[0] = processor.registerContentOperator("Do",
                new IContentOperator() {
                    @Override
                    public void invoke(PdfCanvasProcessor proc,
                            PdfLiteral op, List<PdfObject> operands) {
                        // classifyDo is shared with ContentPreservingTagger;
                        // the two passes MUST agree on every Do or their
                        // parallel form/image index counters desync and all
                        // later tag references on the page shift by one.
                        XObjectKind kind = classifyDo(currentPage, operands);
                        if (kind == XObjectKind.FORM) {
                            listener.flushPendingText();
                            // ALWAYS record the invocation — the writer
                            // counts every FORM Do. BBox is best-effort
                            // (records without bbox stay unmatched, which
                            // degrades safely to Artifact wrapping).
                            Map<String, Object> xobjInfo =
                                new LinkedHashMap<>();
                            xobjInfo.put("index",
                                listener.xobjectInvocations.size());
                            xobjInfo.put("name",
                                ((PdfName) operands.get(0)).getValue());
                            try {
                                PdfStream xobjStream = currentPage
                                    .getResources().getPdfObject()
                                    .getAsDictionary(PdfName.XObject)
                                    .getAsStream((PdfName) operands.get(0));
                                PdfArray bboxArr =
                                    xobjStream.getAsArray(PdfName.BBox);
                                if (bboxArr != null && bboxArr.size() >= 4) {
                                    float bx = bboxArr.getAsNumber(0).floatValue();
                                    float by = bboxArr.getAsNumber(1).floatValue();
                                    float bx2 = bboxArr.getAsNumber(2).floatValue();
                                    float by2 = bboxArr.getAsNumber(3).floatValue();

                                    // Apply XObject's own Matrix if present
                                    PdfArray matArr = xobjStream.getAsArray(
                                        new PdfName("Matrix"));
                                    if (matArr != null && matArr.size() >= 6) {
                                        float a = matArr.getAsNumber(0).floatValue();
                                        float b2 = matArr.getAsNumber(1).floatValue();
                                        float c = matArr.getAsNumber(2).floatValue();
                                        float d = matArr.getAsNumber(3).floatValue();
                                        float e = matArr.getAsNumber(4).floatValue();
                                        float f = matArr.getAsNumber(5).floatValue();
                                        float nx = a * bx + c * by + e;
                                        float ny = b2 * bx + d * by + f;
                                        float nx2 = a * bx2 + c * by2 + e;
                                        float ny2 = b2 * bx2 + d * by2 + f;
                                        bx = Math.min(nx, nx2);
                                        by = Math.min(ny, ny2);
                                        bx2 = Math.max(nx, nx2);
                                        by2 = Math.max(ny, ny2);
                                    }

                                    // Apply current CTM
                                    Matrix ctm = proc.getGraphicsState().getCtm();
                                    float px = ctm.get(Matrix.I11) * bx
                                        + ctm.get(Matrix.I21) * by
                                        + ctm.get(Matrix.I31);
                                    float py = ctm.get(Matrix.I12) * bx
                                        + ctm.get(Matrix.I22) * by
                                        + ctm.get(Matrix.I32);
                                    float px2 = ctm.get(Matrix.I11) * bx2
                                        + ctm.get(Matrix.I21) * by2
                                        + ctm.get(Matrix.I31);
                                    float py2 = ctm.get(Matrix.I12) * bx2
                                        + ctm.get(Matrix.I22) * by2
                                        + ctm.get(Matrix.I32);

                                    // Normalize to CropBox-relative
                                    float x = Math.min(px, px2) - fCropOffsetX;
                                    float y = Math.min(py, py2) - fCropOffsetY;
                                    float w = Math.abs(px2 - px);
                                    float h = Math.abs(py2 - py);

                                    xobjInfo.put("x", round(x));
                                    xobjInfo.put("y", round(y));
                                    xobjInfo.put("width", round(w));
                                    xobjInfo.put("height", round(h));
                                }
                            } catch (Exception ignored) {
                                // BBox extraction failed — keep bbox-less record
                            }
                            listener.xobjectInvocations.add(xobjInfo);
                            return;  // Skip Form XObject — don't recurse
                        }

                        // IMAGE or OTHER — delegate for graphics-state
                        // tracking and image events. Guarded: a malformed
                        // operand or broken image must not abort the whole
                        // page analysis. If an IMAGE Do fails before its
                        // render event fires, add a fallback record so the
                        // image list stays a pure function of the Do
                        // sequence (what the writer counts).
                        listener.flushPendingText();
                        int imagesBefore = listener.images.size();
                        try {
                            if (origDo[0] != null)
                                origDo[0].invoke(proc, op, operands);
                        } catch (RuntimeException e) {
                            System.err.println(
                                "WARNING: Do handler failed during analysis: "
                                + e.getClass().getSimpleName() + ": "
                                + e.getMessage());
                        }
                        if (kind == XObjectKind.IMAGE
                                && listener.images.size() == imagesBefore) {
                            Map<String, Object> img = new LinkedHashMap<>();
                            img.put("index", listener.images.size());
                            img.put("x", 0f);
                            img.put("y", 0f);
                            img.put("width", 0f);
                            img.put("height", 0f);
                            listener.images.add(img);
                        }
                    }
                });

            // Strip invisible text (rendering mode 3) and hidden OCR font
            // text before analysis so operator indices match what the
            // ContentPreservingEditor will see after the same stripping.
            //
            // EXCEPTION: if stripping removes ALL text operators (invisible-
            // only pages — scanned PDFs with Tr=3 OCR overlay), re-analyze
            // with the raw content so the Tr=3 runs are recorded.  The
            // content_matcher will match Docling elements to these runs,
            // and the tagger will remap Tr=3→Tr=0 instead of stripping.
            boolean invisibleOnlyPage = false;
            try {
                byte[] pageBytes = page.getContentBytes();
                boolean stripped = false;
                if (pageBytes != null && pageBytes.length > 0) {
                    // Normalize operator spacing first: iText 9's tokenizer
                    // merges "Tf( )Tj" into unknown keyword "TfTj" when no
                    // whitespace separates operator from string delimiter.
                    // Must run before other preprocessing for correct
                    // line-by-line font tracking in downstream steps.
                    byte[] cleaned = PdfAccessibilityTagger
                        .normalizeOperatorSpacing(pageBytes);
                    cleaned = PdfAccessibilityTagger.stripInvisibleText(cleaned);
                    PdfDictionary pageRes = page.getResources() != null
                        ? page.getResources().getPdfObject() : null;
                    if (pageRes != null) {
                        Set<String> hidden = PdfAccessibilityTagger
                            .findHiddenOCRFontNames(pageRes);
                        if (!hidden.isEmpty()) {
                            cleaned = PdfAccessibilityTagger
                                .stripFontText(cleaned, hidden);
                        }
                    }
                    cleaned = PdfAccessibilityTagger.fixFontInheritance(cleaned);
                    if (!java.util.Arrays.equals(cleaned, pageBytes)) {
                        // Process cleaned content instead of raw page
                        processor.processContent(cleaned, page.getResources());
                        stripped = true;
                        listener.flushPendingText();

                        // If stripping removed ALL text, this is an invisible-
                        // only page (scanned PDF with Tr=3 OCR).  Re-analyze
                        // with raw content so the Tr=3 runs are recorded.
                        if (listener.textRuns.isEmpty()) {
                            // Check raw content actually had text operators
                            String rawStr = new String(pageBytes,
                                java.nio.charset.StandardCharsets.ISO_8859_1);
                            if (rawStr.contains("Tj") || rawStr.contains("TJ")) {
                                // Reset listener and re-analyze with raw content
                                listener.textRuns.clear();
                                listener.images.clear();
                                listener.xobjectInvocations.clear();
                                listener.runIndex = 0;
                                listener.pendingText = null;
                                PdfCanvasProcessor rawProcessor =
                                    new PdfCanvasProcessor(listener);
                                // Re-register operator wrappers for run indexing
                                for (String textOp : new String[]{
                                        "Tj", "TJ", "'", "\""}) {
                                    final IContentOperator[] oh =
                                        new IContentOperator[1];
                                    oh[0] = rawProcessor.registerContentOperator(
                                        textOp, new IContentOperator() {
                                        @Override
                                        public void invoke(
                                                PdfCanvasProcessor proc,
                                                PdfLiteral op,
                                                List<PdfObject> operands) {
                                            listener.beginTextOperator();
                                            if (oh[0] != null)
                                                oh[0].invoke(proc, op, operands);
                                            listener.endTextOperator();
                                        }
                                    });
                                }
                                // Skip Form XObject recursion (same as main)
                                rawProcessor.registerContentOperator("Do",
                                    new IContentOperator() {
                                    @Override
                                    public void invoke(
                                            PdfCanvasProcessor proc,
                                            PdfLiteral op,
                                            List<PdfObject> operands) {
                                        // no-op: skip XObject recursion
                                    }
                                });
                                rawProcessor.processContent(
                                    PdfAccessibilityTagger
                                        .normalizeOperatorSpacing(pageBytes),
                                    page.getResources());
                                listener.flushPendingText();

                                // Broken .notdef-only fonts (NotDefSpecial)
                                // are now handled by Python preprocessing
                                // (_preprocess_broken_fonts) which replaces
                                // them with DejaVuSans before analysis.
                                // If a broken font somehow reaches here, the
                                // runs are still usable — the tagger's remap
                                // path will handle Tr=3→Tr=0.
                                invisibleOnlyPage = true;
                                System.err.println("  Page " + p
                                    + ": invisible-only — re-analyzed"
                                    + " with raw content ("
                                    + listener.textRuns.size()
                                    + " Tr=3 runs)");
                            }
                        } else {
                            System.err.println("  Page " + p
                                + ": analyzed with invisible text stripped ("
                                + pageBytes.length + " -> " + cleaned.length
                                + " bytes)");
                        }
                    }
                }
                if (!stripped) {
                    // Use processContent with normalized bytes instead of
                    // processPageContent to ensure operator spacing fix.
                    byte[] rawBytes = page.getContentBytes();
                    if (rawBytes != null && rawBytes.length > 0) {
                        processor.processContent(
                            PdfAccessibilityTagger
                                .normalizeOperatorSpacing(rawBytes),
                            page.getResources());
                    } else {
                        processor.processPageContent(page);
                    }
                }
            } catch (Exception e) {
                System.err.println("  Warning: page " + p + " analysis failed: "
                    + e.getMessage());
            }
            listener.flushPendingText();  // Flush final pending text

            Map<String, Object> pageData = new LinkedHashMap<>();
            pageData.put("page", p);

            // Page dimensions (CropBox-relative) and rotation
            pageData.put("width", round(reportWidth));
            pageData.put("height", round(reportHeight));
            int rotation = page.getRotation();
            pageData.put("rotation", rotation);
            pageData.put("invisible_only", invisibleOnlyPage);
            pageData.put("has_crop_box", hasCropBox);
            if (hasCropBox) {
                pageData.put("crop_offset_x", round(cropOffsetX));
                pageData.put("crop_offset_y", round(cropOffsetY));
                System.err.println("  Page " + p + ": CropBox offset ("
                    + round(cropOffsetX) + ", " + round(cropOffsetY)
                    + "), visible area " + round(reportWidth) + "x"
                    + round(reportHeight));
            }

            // Existing marked content info
            pageData.put("has_existing_mcids", listener.hasExistingMCIDs);
            pageData.put("existing_mcid_count", listener.existingMCIDCount);

            // Text runs — keep all runs with original indices (the editor's
            // textRunIndex must stay aligned with operator order).  Runs
            // outside CropBox have CropBox-relative coords far from any
            // Docling element, so the matcher naturally won't assign them.
            // They become Artifacts, which is correct.
            pageData.put("text_run_count", listener.textRuns.size());
            pageData.put("text_runs", listener.textRuns);

            // Count out-of-bounds runs for diagnostic logging
            if (hasCropBox) {
                float tol = 50f;
                int outOfBounds = 0;
                for (Map<String, Object> run : listener.textRuns) {
                    float rx = ((Number) run.get("x")).floatValue();
                    float ry = ((Number) run.get("y")).floatValue();
                    if (rx < -tol || rx > reportWidth + tol
                            || ry < -tol || ry > reportHeight + tol) {
                        outOfBounds++;
                    }
                }
                if (outOfBounds > 0) {
                    pageData.put("out_of_bounds_runs", outOfBounds);
                    System.err.println("  Page " + p + ": " + outOfBounds
                        + "/" + listener.textRuns.size()
                        + " text runs outside CropBox (will become Artifact)");
                }
            }

            // Images
            pageData.put("image_count", listener.images.size());
            pageData.put("images", listener.images);

            // Form XObject invocations (for 7.20 t2 compliance)
            pageData.put("xobject_count", listener.xobjectInvocations.size());
            if (!listener.xobjectInvocations.isEmpty()) {
                pageData.put("xobject_invocations",
                    listener.xobjectInvocations);
            }

            // Summary stats
            int totalChars = 0;
            Set<String> fontsUsed = new HashSet<>();
            for (Map<String, Object> run : listener.textRuns) {
                String text = (String) run.get("text");
                if (text != null) totalChars += text.length();
                String font = (String) run.get("font");
                String ftype = (String) run.get("font_type");
                if (font != null) fontsUsed.add(font + " (" + (ftype != null ? ftype : "?") + ")");
            }
            pageData.put("total_chars", totalChars);
            pageData.put("fonts_used", new ArrayList<>(fontsUsed));

            pageAnalyses.add(pageData);
        }

        result.put("page_analyses", pageAnalyses);

        // Document-level summary
        int totalRuns = 0;
        int totalImages = 0;
        int totalChars = 0;
        Set<String> allFonts = new HashSet<>();
        int pagesWithMCIDs = 0;

        for (Map<String, Object> pa : pageAnalyses) {
            totalRuns += (int) pa.get("text_run_count");
            totalImages += (int) pa.get("image_count");
            totalChars += (int) pa.get("total_chars");
            @SuppressWarnings("unchecked")
            List<String> fonts = (List<String>) pa.get("fonts_used");
            allFonts.addAll(fonts);
            if ((boolean) pa.get("has_existing_mcids")) pagesWithMCIDs++;
        }

        Map<String, Object> summary = new LinkedHashMap<>();
        summary.put("total_text_runs", totalRuns);
        summary.put("total_images", totalImages);
        summary.put("total_chars", totalChars);
        summary.put("total_fonts", allFonts.size());
        summary.put("all_fonts", new ArrayList<>(allFonts));
        summary.put("pages_with_existing_mcids", pagesWithMCIDs);
        result.put("summary", summary);

        pdfDoc.close();

        Gson gson = new GsonBuilder().setPrettyPrinting().create();
        return gson.toJson(result);
    }

    /**
     * Write analysis to a file.
     */
    public static void analyzeToFile(String inputPath, String outputPath)
            throws Exception {
        String json = analyze(inputPath);
        try (FileWriter fw = new FileWriter(outputPath, StandardCharsets.UTF_8)) {
            fw.write(json);
        }
        System.out.println("Content stream analysis written to: " + outputPath);
    }

    // ---------------------------------------------------------------
    // IEventListener that collects text runs and images
    // ---------------------------------------------------------------

    static class PageAnalysisListener implements IEventListener {
        final List<Map<String, Object>> textRuns = new ArrayList<>();
        final List<Map<String, Object>> images = new ArrayList<>();
        final List<Map<String, Object>> xobjectInvocations = new ArrayList<>();
        boolean hasExistingMCIDs = false;
        int existingMCIDCount = 0;

        // CropBox normalization: subtract from all coordinates to
        // convert from MediaBox space to CropBox-relative space.
        float cropOffsetX = 0, cropOffsetY = 0;

        // Operator-level aggregation: TJ array segments are merged
        // into a single run per operator (Tj/TJ/'/" boundary).
        // Package-private for reset access from analyzeToFile().
        String pendingText = null;
        float pendingX = 0, pendingY = 0;
        float pendingEndX = 0;
        float pendingFontSize = 0;
        String pendingFont = null;
        String pendingFontType = null;
        int pendingRenderMode = 0;
        boolean pendingHasUnicode = true;
        int runIndex = 0;
        boolean inOperator = false;

        @Override
        public void eventOccurred(IEventData data, EventType type) {
            if (type == EventType.RENDER_TEXT) {
                handleText((TextRenderInfo) data);
            } else if (type == EventType.RENDER_IMAGE) {
                flushPendingText();  // flush before image
                handleImage((ImageRenderInfo) data);
            }
        }

        @Override
        public Set<EventType> getSupportedEvents() {
            return new HashSet<>(Arrays.asList(
                EventType.RENDER_TEXT,
                EventType.RENDER_IMAGE
            ));
        }

        void beginTextOperator() {
            flushPendingText();  // Flush previous operator's aggregated text
            inOperator = true;
        }

        void endTextOperator() {
            flushPendingText();  // Flush this operator's aggregated text
            inOperator = false;
        }

        private void handleText(TextRenderInfo tri) {
            String text = tri.getText();
            if (text == null) return;

            // Get position from baseline
            LineSegment baseline = tri.getBaseline();
            Vector start = baseline.getStartPoint();
            Vector end = baseline.getEndPoint();

            float x = start.get(Vector.I1);
            float y = start.get(Vector.I2);
            float endX = end.get(Vector.I1);

            // Font info
            float fontSize = 0;
            String fontName = "unknown";
            String fontType = "unknown";
            try {
                var gs = tri.getGraphicsState();
                if (gs != null) {
                    fontSize = gs.getFontSize();
                    var font = gs.getFont();
                    if (font != null) {
                        // Try multiple ways to get font name
                        var prog = font.getFontProgram();
                        if (prog != null && prog.getFontNames() != null) {
                            String name = prog.getFontNames().getFontName();
                            if (name != null && !name.isEmpty()) {
                                fontName = name;
                            }
                        }
                        // Fallback: get from PDF object
                        if ("unknown".equals(fontName)) {
                            var pdfFont = font.getPdfObject();
                            if (pdfFont != null) {
                                var baseFont = pdfFont.getAsName(PdfName.BaseFont);
                                if (baseFont != null) {
                                    fontName = baseFont.getValue();
                                }
                            }
                        }
                        // Detect font type
                        var pdfFont = font.getPdfObject();
                        if (pdfFont != null) {
                            var subtype = pdfFont.getAsName(PdfName.Subtype);
                            if (subtype != null) {
                                fontType = subtype.getValue();
                            }
                        }
                    }
                }
            } catch (Exception e) {
                // Font info extraction can fail for some fonts
            }

            // Text rendering mode
            int renderMode = 0;
            try {
                renderMode = tri.getGraphicsState().getTextRenderingMode();
            } catch (Exception ignored) {}

            // Check if this text has valid Unicode mapping
            boolean hasUnicode = true;
            if (text.contains("\ufffd") || text.contains("\u0000")) {
                hasUnicode = false;
            }
            // PUA characters suggest missing ToUnicode
            for (int i = 0; i < text.length(); i++) {
                int cp = text.codePointAt(i);
                if (cp >= 0xE000 && cp <= 0xF8FF) {
                    hasUnicode = false;
                    break;
                }
            }

            // Check for existing MCIDs in the content stream
            try {
                int mcid = tri.getMcid();
                if (mcid >= 0) {
                    hasExistingMCIDs = true;
                    existingMCIDCount++;
                }
            } catch (Exception ignored) {}

            // Operator-level aggregation: when inside a tracked text
            // operator (Tj/TJ/'/" wrapped by beginTextOperator/
            // endTextOperator), aggregate all RENDER_TEXT events from
            // the same operator into one run.  This produces one run
            // index per operator, matching the editor's textRunIndex
            // which increments once per operator write() call.
            // Falls back to one-run-per-event if a RENDER_TEXT fires
            // outside operator tracking (safety net).
            if (inOperator && pendingText != null) {
                // Aggregate within same operator: append text, extend end
                pendingText += text;
                pendingEndX = endX;
                if (!hasUnicode) pendingHasUnicode = false;
            } else {
                // New operator or outside tracking: flush previous, start new
                flushPendingText();
                pendingText = text;
                pendingX = x;
                pendingY = y;
                pendingEndX = endX;
                pendingFontSize = fontSize;
                pendingFont = fontName;
                pendingFontType = fontType;
                pendingRenderMode = renderMode;
                pendingHasUnicode = hasUnicode;
            }
        }

        void flushPendingText() {
            if (pendingText == null || pendingText.isEmpty()) return;

            // Apply CropBox offset to normalize coordinates
            float normX = pendingX - cropOffsetX;
            float normY = pendingY - cropOffsetY;
            float normEndX = pendingEndX - cropOffsetX;

            Map<String, Object> run = new LinkedHashMap<>();
            run.put("index", runIndex++);
            run.put("text", pendingText);
            run.put("x", round(normX));
            run.put("y", round(normY));
            run.put("end_x", round(normEndX));
            run.put("width", round(normEndX - normX));
            run.put("font", pendingFont != null ? pendingFont : "unknown");
            run.put("font_type", pendingFontType != null ? pendingFontType : "unknown");
            run.put("font_size", round(pendingFontSize));
            run.put("render_mode", pendingRenderMode);
            run.put("has_unicode", pendingHasUnicode);
            run.put("char_count", pendingText.length());

            textRuns.add(run);
            pendingText = null;
        }

        private void handleImage(ImageRenderInfo iri) {
            try {
                Matrix ctm = iri.getImageCtm();
                float x = ctm.get(Matrix.I31) - cropOffsetX;
                float y = ctm.get(Matrix.I32) - cropOffsetY;
                float width = ctm.get(Matrix.I11);
                float height = ctm.get(Matrix.I22);

                Map<String, Object> img = new LinkedHashMap<>();
                img.put("index", images.size());
                img.put("x", round(x));
                img.put("y", round(y));
                img.put("width", round(Math.abs(width)));
                img.put("height", round(Math.abs(height)));

                // Try to get image name from resources
                try {
                    var imgObj = iri.getImage();
                    if (imgObj != null) {
                        var pdfObj = imgObj.getPdfObject();
                        if (pdfObj != null) {
                            img.put("bits_per_component",
                                pdfObj.getAsInt(PdfName.BitsPerComponent));
                        }
                    }
                } catch (Exception ignored) {}

                images.add(img);
            } catch (Exception e) {
                // Image extraction can fail for inline images
            }
        }
    }

    private static float round(float v) {
        return Math.round(v * 100f) / 100f;
    }

}
