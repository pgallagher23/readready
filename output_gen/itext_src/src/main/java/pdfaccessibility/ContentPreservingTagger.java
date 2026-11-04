// SPDX-License-Identifier: AGPL-3.0-or-later
/*
 * ContentPreservingTagger — Phase 3: Tag existing content in-place.
 *
 * Instead of creating invisible overlay text, this tagger wraps the
 * original visible content stream operators in BDC/EMC markers, so
 * the tagged content IS the visible content.
 *
 * Architecture:
 *   Step 1: Pre-create structure tree using TagTreePointer
 *   Step 2: Edit content streams using PdfCanvasEditor, injecting
 *           BDC/EMC via canvas.openTag()/closeTag() which registers
 *           MCIDs with iText's TagStructureContext
 *   Step 3: iText builds correct ParentTree on pdfDoc.close()
 */
package pdfaccessibility;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.itextpdf.io.font.PdfEncodings;
import com.itextpdf.kernel.font.PdfFont;
import com.itextpdf.kernel.geom.Rectangle;
import com.itextpdf.kernel.pdf.*;
import com.itextpdf.kernel.pdf.canvas.CanvasTag;
import com.itextpdf.kernel.pdf.canvas.PdfCanvas;
import com.itextpdf.kernel.pdf.canvas.parser.PdfCanvasProcessor;
import com.itextpdf.kernel.pdf.filespec.PdfFileSpec;
import com.itextpdf.kernel.pdf.annot.PdfAnnotation;
import com.itextpdf.kernel.pdf.annot.PdfLinkAnnotation;
import com.itextpdf.kernel.pdf.tagging.PdfObjRef;
import com.itextpdf.kernel.pdf.tagging.PdfStructElem;
import com.itextpdf.kernel.pdf.tagutils.TagTreePointer;
import com.itextpdf.kernel.pdf.tagutils.TagReference;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.*;

public class ContentPreservingTagger {

    public static void tag(String inputPath, String enrichedPath,
                            String outputPath, String lang, String title)
            throws Exception {

        // --- Load enriched structure ---
        String jsonStr = new String(
            Files.readAllBytes(Paths.get(enrichedPath)), StandardCharsets.UTF_8);
        JsonObject structure = JsonParser.parseString(jsonStr).getAsJsonObject();
        JsonArray elements = structure.getAsJsonArray("elements");

        // Extract title early (needed for metadata-only path too)
        if (title == null || title.isEmpty()) {
            JsonObject metadata = structure.getAsJsonObject("metadata");
            if (metadata != null && metadata.has("title")
                    && !metadata.get("title").isJsonNull()) {
                title = metadata.get("title").getAsString();
            }
        }
        if (title == null || title.isEmpty()) title = "Untitled Document";

        if (elements == null || elements.size() == 0) {
            System.err.println("No elements — applying metadata-only fixes.");
            PdfDocument pdfDoc = new OutputPdfDocument(
                new PdfReader(inputPath), new PdfWriter(outputPath));
            pdfDoc.setTagged();
            if (lang != null && !lang.isEmpty()) {
                pdfDoc.getCatalog().setLang(new PdfString(lang));
            }

            // PDF/UA-1: MarkInfo, ViewerPreferences, Tabs
            pdfDoc.getCatalog().put(
                PdfName.MarkInfo,
                new PdfDictionary(java.util.Map.of(PdfName.Marked, PdfBoolean.TRUE))
            );
            PdfViewerPreferences viewerPrefs = new PdfViewerPreferences();
            viewerPrefs.setDisplayDocTitle(true);
            pdfDoc.getCatalog().setViewerPreferences(viewerPrefs);
            if (title != null && !title.isEmpty()) {
                pdfDoc.getDocumentInfo().setTitle(title);
            }
            for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
                pdfDoc.getPage(p).put(PdfName.Tabs, PdfName.S);
            }

            // 7.1.3: wrap all page content as Artifact (no semantic elements)
            for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
                PdfPage page = pdfDoc.getPage(p);
                byte[] orig = page.getContentBytes();
                if (orig == null || orig.length == 0) continue;
                java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
                bos.write("/Artifact BMC\n".getBytes(java.nio.charset.StandardCharsets.US_ASCII));
                bos.write(orig);
                bos.write("\nEMC\n".getBytes(java.nio.charset.StandardCharsets.US_ASCII));
                page.put(PdfName.Contents,
                    new PdfStream(bos.toByteArray()).makeIndirect(pdfDoc));
            }
            System.out.println("  Wrapped all page content as Artifact (0 elements)");

            PdfAccessibilityTagger.addMissingToUnicodeCMaps(pdfDoc);
            PdfAccessibilityTagger.stripIncorrectCIDSets(pdfDoc);
            PdfAccessibilityTagger.stripSymbolicTrueTypeEncoding(pdfDoc);
            PdfAccessibilityTagger.fixCIDToGIDMap(pdfDoc);
            PdfAccessibilityTagger.embedMissingFonts(pdfDoc);
            PdfAccessibilityTagger.checkFontCompliance(pdfDoc);
            PdfAccessibilityTagger.fixOCProperties(pdfDoc);
            PdfAccessibilityTagger.fixEmbeddedFileSpecs(pdfDoc);
            PdfAccessibilityTagger.setXmpMetadata(pdfDoc, title, lang);
            pdfDoc.close();
            System.out.println("Metadata-only pass complete: " + outputPath);
            return;
        }

        // --- Parse overlay pages (scanned/image-only pages needing overlay fallback) ---
        Set<Integer> overlayPages = new HashSet<>();
        if (structure.has("overlay_pages")) {
            JsonArray op = structure.getAsJsonArray("overlay_pages");
            for (JsonElement pe : op) {
                overlayPages.add(pe.getAsInt());
            }
        }
        if (!overlayPages.isEmpty()) {
            System.out.println("  Overlay fallback pages: " + overlayPages);
        }

        // --- Parse form-image pages (image-based forms with widgets) ---
        Set<Integer> formImagePages = new HashSet<>();
        if (structure.has("form_image_pages")) {
            JsonArray fip = structure.getAsJsonArray("form_image_pages");
            for (JsonElement pe : fip) {
                formImagePages.add(pe.getAsInt());
            }
        }
        if (!formImagePages.isEmpty()) {
            System.out.println("  Form-image pages: " + formImagePages);
        }

        // --- Parse invisible-only pages (Tr=3 OCR text, no visible text) ---
        // These pages have content_run_indices from the analyzer's raw-content
        // re-analysis.  Instead of stripping the Tr=3 text, we keep it and
        // have ContentPreservingEditor re-assert Tr=3 at the start of every
        // new BDC/Artifact scope it opens, but only while the source's own
        // prevailing Tr is already 3 (see ContentPreservingEditor.forceInvisible
        // and .sourceTr).  Tr is graphics state and BDC/EMC does not scope it,
        // so this re-assertion is state-neutral by construction — it keeps
        // each marked-content scope self-contained without ever hiding
        // content the source draws visibly.
        Set<Integer> invisibleOnlyPages = new HashSet<>();
        if (structure.has("invisible_only_pages")) {
            JsonArray iop = structure.getAsJsonArray("invisible_only_pages");
            for (JsonElement pe : iop) {
                invisibleOnlyPages.add(pe.getAsInt());
            }
        }
        if (!invisibleOnlyPages.isEmpty()) {
            System.out.println("  Invisible-only pages (Tr=3 forced): "
                + invisibleOnlyPages);
        }

        // --- Parse elements and build per-page run mapping ---
        List<ElementInfo> allElements = new ArrayList<>();
        Map<Integer, Map<Integer, ElementInfo>> pageRunMap = new HashMap<>();
        // page (1-based) -> link annotations on that page (assigned after pdfDoc opens)
        Map<Integer, List<LinkRect>> pageLinkRects;
        // packRunKey(page, runIdx) -> {x, y} display-space run position
        Map<Long, float[]> runXY = new HashMap<>();
        // Harvest run positions from EVERY content_run_indices array anywhere in
        // the structure JSON (top-level elements + nested TOCI children, table
        // cells, list items) so geometric link matching can locate TOC/cell runs
        // that the top-level run→element loop below skips.
        harvestRunPositions(elements, runXY);
        Map<Integer, Map<Integer, ElementInfo>> pageImageMap = new HashMap<>();
        Map<Integer, Map<Integer, ElementInfo>> pageXobjectMap = new HashMap<>();
        // Track which elements have content runs
        Set<String> elementsWithRuns = new HashSet<>();
        // Diagnostic: runs rejected by the bbox containment guard below
        // (a non-degenerate split bbox should land its runs inside
        // tolerance; a spike here flags upstream split-geometry bugs).
        int containmentRejected = 0;

        for (JsonElement je : elements) {
            JsonObject elem = je.getAsJsonObject();
            String tag = elem.has("tag") ? elem.get("tag").getAsString() : "P";
            String text = elem.has("text") ? PdfAccessibilityTagger.dePuaText(elem.get("text").getAsString()) : "";
            String altText = (elem.has("alt_text") && !elem.get("alt_text").isJsonNull())
                ? PdfAccessibilityTagger.dePuaText(elem.get("alt_text").getAsString()) : null;
            int pageNo = elem.has("page_no") ? elem.get("page_no").getAsInt() : 1;
            String id = elem.has("id") ? elem.get("id").getAsString() : "";

            ElementInfo info = new ElementInfo(id, tag, text, altText, pageNo);

            // MathML for Formula Associated File (raw XML — do NOT dePua it)
            if (elem.has("mathml") && !elem.get("mathml").isJsonNull()) {
                info.mathml = elem.get("mathml").getAsString();
            }

            // Table caption (Docling-extracted or VLM-generated) — feeds the
            // /Summary table attribute and (Task 2) the Caption struct element.
            if (elem.has("caption") && !elem.get("caption").isJsonNull()) {
                String cap = PdfAccessibilityTagger.dePuaText(
                    elem.get("caption").getAsString()).trim();
                if (!cap.isEmpty()) info.caption = cap;
            }

            // Parse bbox for overlay fallback positioning
            if (elem.has("bbox") && !elem.get("bbox").isJsonNull()) {
                JsonObject bb = elem.getAsJsonObject("bbox");
                info.bboxL = bb.has("l") ? bb.get("l").getAsFloat() : 72;
                info.bboxB = bb.has("b") ? bb.get("b").getAsFloat() : 72;
                info.bboxR = bb.has("r") ? bb.get("r").getAsFloat() : info.bboxL + 100;
                info.bboxT = bb.has("t") ? bb.get("t").getAsFloat() : info.bboxB + 20;
                info.hasBbox = true;
            }

            allElements.add(info);

            // Parse cells[] for Table elements
            if ("Table".equals(tag) && elem.has("cells")) {
                JsonArray cellsArr = elem.getAsJsonArray("cells");
                List<CellInfo> cellList = new ArrayList<>();
                for (JsonElement ce : cellsArr) {
                    JsonObject c = ce.getAsJsonObject();
                    int row = c.has("row") ? c.get("row").getAsInt() : 0;
                    int col = c.has("col") ? c.get("col").getAsInt() : 0;
                    int rs = c.has("row_span") ? c.get("row_span").getAsInt() : 1;
                    int cs = c.has("col_span") ? c.get("col_span").getAsInt() : 1;
                    String cText = c.has("text") ? PdfAccessibilityTagger.dePuaText(c.get("text").getAsString()) : "";
                    boolean isHdr = c.has("is_header") && c.get("is_header").getAsBoolean();
                    boolean isColHdr = c.has("is_col_header") && c.get("is_col_header").getAsBoolean();
                    boolean isRowHdr = c.has("is_row_header") && c.get("is_row_header").getAsBoolean();
                    // Parse cell-level content_run_indices
                    List<int[]> cellRuns = new ArrayList<>();
                    if (c.has("content_run_indices")) {
                        for (JsonElement cre : c.getAsJsonArray("content_run_indices")) {
                            JsonObject cr = cre.getAsJsonObject();
                            cellRuns.add(new int[]{
                                cr.get("page").getAsInt(),
                                cr.get("run_index").getAsInt()
                            });
                        }
                    }
                    CellInfo ci = new CellInfo(row, col, rs, cs, cText,
                        isHdr, isColHdr, isRowHdr, cellRuns, pageNo);
                    ci.cellId = id + "_r" + row + "c" + col;
                    if (c.has("bbox")) {
                        JsonObject cbb = c.getAsJsonObject("bbox");
                        ci.bboxL = cbb.get("l").getAsFloat();
                        ci.bboxB = cbb.get("b").getAsFloat();
                        ci.bboxR = cbb.get("r").getAsFloat();
                        ci.bboxT = cbb.get("t").getAsFloat();
                        ci.hasBbox = true;
                    }
                    cellList.add(ci);
                }
                info.cells = cellList;
            }

            // Parse children[] for L (List) elements → LI hierarchy
            if ("L".equals(tag) && elem.has("children")) {
                JsonArray childArr = elem.getAsJsonArray("children");
                List<ListItemInfo> liList = new ArrayList<>();
                int liIdx = 0;
                for (JsonElement ce : childArr) {
                    JsonObject c = ce.getAsJsonObject();
                    if (!"LI".equals(c.has("tag") ? c.get("tag").getAsString() : ""))
                        continue;
                    String liText = c.has("text") ? PdfAccessibilityTagger.dePuaText(c.get("text").getAsString()) : "";
                    // Extract label from children
                    String label = null;
                    String bodyText = liText;
                    if (c.has("children")) {
                        for (JsonElement gc : c.getAsJsonArray("children")) {
                            JsonObject gco = gc.getAsJsonObject();
                            String gTag = gco.has("tag") ? gco.get("tag").getAsString() : "";
                            if ("Lbl".equals(gTag))
                                label = gco.has("text") ? PdfAccessibilityTagger.dePuaText(gco.get("text").getAsString()) : null;
                            if ("LBody".equals(gTag))
                                bodyText = gco.has("text") ? PdfAccessibilityTagger.dePuaText(gco.get("text").getAsString()) : liText;
                        }
                    }
                    // Parse LI-level content_run_indices
                    List<int[]> liRuns = new ArrayList<>();
                    if (c.has("content_run_indices")) {
                        for (JsonElement re : c.getAsJsonArray("content_run_indices")) {
                            JsonObject rr = re.getAsJsonObject();
                            liRuns.add(new int[]{
                                rr.get("page").getAsInt(),
                                rr.get("run_index").getAsInt()
                            });
                        }
                    }
                    ListItemInfo li = new ListItemInfo(bodyText, label, liRuns);
                    li.liId = id + "_li" + liIdx;
                    if (c.has("bbox")) {
                        JsonObject bb = c.getAsJsonObject("bbox");
                        li.bboxL = bb.get("l").getAsFloat();
                        li.bboxB = bb.get("b").getAsFloat();
                        li.bboxR = bb.get("r").getAsFloat();
                        li.bboxT = bb.get("t").getAsFloat();
                        li.hasBbox = true;
                    }
                    liList.add(li);
                    liIdx++;
                }
                if (!liList.isEmpty()) {
                    info.listItems = liList;
                }
            }

            // Parse children[] for TOC elements → TOCI hierarchy
            if ("TOC".equals(tag) && elem.has("children")) {
                JsonArray childArr = elem.getAsJsonArray("children");
                List<TocItemInfo> tocList = new ArrayList<>();
                int tociIdx = 0;
                boolean anyChildHasRuns = false;
                for (JsonElement ce : childArr) {
                    JsonObject c = ce.getAsJsonObject();
                    if (!"TOCI".equals(c.has("tag") ? c.get("tag").getAsString() : ""))
                        continue;
                    String tociText = c.has("text") ? PdfAccessibilityTagger.dePuaText(c.get("text").getAsString()) : "";
                    TocItemInfo ti = new TocItemInfo(tociText, new ArrayList<>());
                    ti.tociId = id + "_toci" + tociIdx;
                    // Parse per-TOCI bbox for spatial run assignment (fallback)
                    if (c.has("bbox") && !c.get("bbox").isJsonNull()) {
                        JsonObject bb = c.getAsJsonObject("bbox");
                        ti.bbox = new float[]{
                            bb.has("l") ? bb.get("l").getAsFloat() : 0,
                            bb.has("b") ? bb.get("b").getAsFloat() : 0,
                            bb.has("r") ? bb.get("r").getAsFloat() : 0,
                            bb.has("t") ? bb.get("t").getAsFloat() : 0,
                        };
                    }
                    // Read per-TOCI content_run_indices (from Python content_matcher)
                    if (c.has("content_run_indices")) {
                        for (JsonElement cre : c.getAsJsonArray("content_run_indices")) {
                            JsonObject cr = cre.getAsJsonObject();
                            ti.runRefs.add(new int[]{
                                cr.get("page").getAsInt(),
                                cr.get("run_index").getAsInt()
                            });
                        }
                        if (!ti.runRefs.isEmpty()) anyChildHasRuns = true;
                    }
                    tocList.add(ti);
                    tociIdx++;
                }
                // Fallback: if no TOCI has pre-assigned content_run_indices,
                // distribute parent TOC's runs via spatial bbox matching
                // (backward compat with pre-enriched structure JSONs).
                if (!anyChildHasRuns && !tocList.isEmpty()
                        && elem.has("content_run_indices")) {
                    JsonArray runs = elem.getAsJsonArray("content_run_indices");
                    boolean hasBboxes = tocList.stream()
                        .anyMatch(ti -> ti.bbox != null);
                    for (int ri = 0; ri < runs.size(); ri++) {
                        JsonObject rr = runs.get(ri).getAsJsonObject();
                        int runPage = rr.get("page").getAsInt();
                        int runIdx = rr.get("run_index").getAsInt();
                        int bestToci = ri % tocList.size(); // fallback
                        if (hasBboxes && rr.has("x") && rr.has("y")) {
                            float rx = rr.get("x").getAsFloat();
                            float ry = rr.get("y").getAsFloat();
                            for (int ti = 0; ti < tocList.size(); ti++) {
                                float[] bb = tocList.get(ti).bbox;
                                if (bb != null
                                        && rx >= bb[0] && rx <= bb[2]
                                        && ry >= bb[1] && ry <= bb[3]) {
                                    bestToci = ti;
                                    break;
                                }
                            }
                        }
                        tocList.get(bestToci).runRefs.add(
                            new int[]{runPage, runIdx});
                    }
                }
                if (!tocList.isEmpty()) {
                    info.tocItems = tocList;
                }
            }

            // Parse children[] for P elements with Link children
            if ("P".equals(tag) && elem.has("children")) {
                JsonArray childArr = elem.getAsJsonArray("children");
                boolean hasLink = false;
                for (JsonElement ce : childArr) {
                    JsonObject c = ce.getAsJsonObject();
                    String cTag = c.has("tag") ? c.get("tag").getAsString() : "";
                    if ("Link".equals(cTag)) { hasLink = true; break; }
                }
                if (hasLink) {
                    List<ParagraphChildInfo> pChildren = new ArrayList<>();
                    for (JsonElement ce : childArr) {
                        JsonObject c = ce.getAsJsonObject();
                        String cTag = c.has("tag") ? c.get("tag").getAsString() : "Span";
                        String cText = c.has("text")
                            ? PdfAccessibilityTagger.dePuaText(c.get("text").getAsString()) : "";
                        pChildren.add(new ParagraphChildInfo(cTag, cText));
                    }
                    if (!pChildren.isEmpty()) {
                        info.paragraphChildren = pChildren;
                        // Capture parent P's runs with text so addParagraphWithLinks
                        // can distribute them across children.
                        if (elem.has("content_run_indices")) {
                            List<RunRef> refs = new ArrayList<>();
                            for (JsonElement re : elem.getAsJsonArray("content_run_indices")) {
                                JsonObject r = re.getAsJsonObject();
                                int page = r.get("page").getAsInt();
                                int runIdx = r.get("run_index").getAsInt();
                                String t = r.has("text") ? r.get("text").getAsString() : "";
                                refs.add(new RunRef(page, runIdx, t));
                            }
                            // Reading order: by page then run_index.
                            refs.sort((a, b) -> {
                                if (a.page != b.page) return Integer.compare(a.page, b.page);
                                return Integer.compare(a.runIdx, b.runIdx);
                            });
                            info.parentRunRefs = refs;
                        }
                    }
                }
            }

            // Always capture parent P runs (needed for the geometric link peel
            // on plain paragraphs / Span-only P's, not just Docling-Link
            // anchors).  When the hasLink block above already populated
            // parentRunRefs, leave it untouched.
            if ("P".equals(tag) && elem.has("content_run_indices")
                    && info.parentRunRefs == null) {
                List<RunRef> refs = new ArrayList<>();
                for (JsonElement re : elem.getAsJsonArray("content_run_indices")) {
                    JsonObject r = re.getAsJsonObject();
                    int page = r.get("page").getAsInt();
                    int runIdx = r.get("run_index").getAsInt();
                    String t = r.has("text") ? r.get("text").getAsString() : "";
                    refs.add(new RunRef(page, runIdx, t));
                }
                // Reading order: by page then run_index.
                refs.sort((a, b) -> {
                    if (a.page != b.page) return Integer.compare(a.page, b.page);
                    return Integer.compare(a.runIdx, b.runIdx);
                });
                info.parentRunRefs = refs;
            }

            // Map content runs → element (for non-Table, non-L, non-TOC elements).
            // Table cell, LI, and TOCI runs are mapped separately in structure creation.
            if (!"Table".equals(tag) && !"L".equals(tag) && !"TOC".equals(tag)
                    && elem.has("content_run_indices")) {
                JsonArray runs = elem.getAsJsonArray("content_run_indices");
                if (runs.size() > 0) {
                    elementsWithRuns.add(id);
                    for (JsonElement re : runs) {
                        JsonObject runRef = re.getAsJsonObject();
                        int page = runRef.get("page").getAsInt();
                        int runIdx = runRef.get("run_index").getAsInt();

                        // Bbox containment check: reject runs whose position
                        // falls far outside the element's bounding box.
                        // Prevents "run stealing" where page numbers / headers
                        // from a different page region leak into an adjacent
                        // element, shifting MCID assignments and breaking
                        // Acrobat text selection for scanned PDFs.
                        // Tolerance must match the content_matcher's wide pass
                        // (55pt) — the matcher already validated assignments
                        // at that tolerance, so a tighter check here causes
                        // legitimate last-word runs to become artifacts.
                        if (runRef.has("x") && runRef.has("y")) {
                            float runX = runRef.get("x").getAsFloat();
                            float runY = runRef.get("y").getAsFloat();
                            runXY.put(packRunKey(page, runIdx), new float[]{runX, runY});
                            if (info.hasBbox) {
                                float tol = 55.0f; // must match content_matcher wide_tolerance
                                if (runX < info.bboxL - tol || runX > info.bboxR + tol
                                        || runY < info.bboxB - tol || runY > info.bboxT + tol) {
                                    containmentRejected++;
                                    continue;
                                }
                            }
                        }

                        pageRunMap.computeIfAbsent(page, k -> new HashMap<>())
                            .put(runIdx, info);
                    }
                }
            }
            // For Tables with cells that have content runs, mark as having runs
            if ("Table".equals(tag) && info.cells != null) {
                boolean anyCellHasRuns = info.cells.stream()
                    .anyMatch(ci -> !ci.runRefs.isEmpty());
                if (anyCellHasRuns) {
                    elementsWithRuns.add(id);
                }
            }
            // For Lists with LI children that have content runs
            if ("L".equals(tag) && info.listItems != null) {
                boolean anyLiHasRuns = info.listItems.stream()
                    .anyMatch(li -> !li.runRefs.isEmpty());
                if (anyLiHasRuns) {
                    elementsWithRuns.add(id);
                }
            }
            // For TOC with TOCI children that have content runs
            if ("TOC".equals(tag) && info.tocItems != null) {
                boolean anyTociHasRuns = info.tocItems.stream()
                    .anyMatch(ti -> !ti.runRefs.isEmpty());
                if (anyTociHasRuns) {
                    elementsWithRuns.add(id);
                }
            }
            // Parse image_run_indices for Figure elements
            if ("Figure".equals(tag) && elem.has("image_run_indices")) {
                JsonArray imgRuns = elem.getAsJsonArray("image_run_indices");
                for (JsonElement ire : imgRuns) {
                    JsonObject ir = ire.getAsJsonObject();
                    int page = ir.get("page").getAsInt();
                    int imgIdx = ir.get("image_index").getAsInt();
                    pageImageMap.computeIfAbsent(page, k -> new HashMap<>())
                        .put(imgIdx, info);
                    elementsWithRuns.add(id);
                }
            }
            // Parse xobject_content_indices for Form XObject wrapping
            if (elem.has("xobject_content_indices")) {
                JsonArray xobjRuns = elem.getAsJsonArray("xobject_content_indices");
                for (JsonElement xre : xobjRuns) {
                    JsonObject xr = xre.getAsJsonObject();
                    int xobjPage = xr.get("page").getAsInt();
                    int xobjIdx = xr.get("xobject_index").getAsInt();
                    pageXobjectMap.computeIfAbsent(xobjPage, k -> new HashMap<>())
                        .put(xobjIdx, info);
                    elementsWithRuns.add(id);
                }
            }
        }
        if (containmentRejected > 0) {
            System.out.println("  Bbox containment guard rejected "
                + containmentRejected + " run(s) (>55pt outside element bbox)");
        }

        // --- Open PDF ---
        PdfReader reader = new PdfReader(inputPath);
        PdfWriter writer = new PdfWriter(outputPath);
        PdfDocument pdfDoc = new OutputPdfDocument(reader, writer);
        int numPages = pdfDoc.getNumberOfPages();

        // Build per-page link-rect index now that pdfDoc is open (consumed in later tasks).
        pageLinkRects = buildLinkRectIndex(pdfDoc);

        // Enable tagged mode — iText manages ParentTree
        pdfDoc.setTagged();

        // Clear pre-existing structure tree
        clearExistingStructure(pdfDoc);

        // Set metadata
        pdfDoc.getCatalog().setLang(new PdfString(lang));
        pdfDoc.getCatalog().put(PdfName.MarkInfo,
            new PdfDictionary(Map.of(PdfName.Marked, PdfBoolean.TRUE)));
        pdfDoc.getDocumentInfo().setTitle(title);
        var viewerPrefs = new PdfViewerPreferences();
        viewerPrefs.setDisplayDocTitle(true);
        pdfDoc.getCatalog().setViewerPreferences(viewerPrefs);

        for (int p = 1; p <= numPages; p++) {
            pdfDoc.getPage(p).put(PdfName.Tabs, PdfName.S);
        }

        // Snapshot source font refs before overlay font is created
        Set<PdfIndirectReference> sourceFontRefs =
            PdfAccessibilityTagger.snapshotSourceFontRefs(pdfDoc);

        System.out.println("Content-preserving tagging: " + numPages
            + " pages, " + allElements.size() + " elements");

        // ============================================================
        // STEP 1: Pre-create structure tree with TagTreePointer
        // ============================================================
        // Create all structure elements FIRST, collecting TagReferences
        // that will be used in Step 2 for content stream editing.

        TagTreePointer tagPointer = pdfDoc.getTagStructureContext()
            .getAutoTaggingPointer();

        // Map element_id -> position in tag tree for navigation
        // We record the path (child index at each level) so we can
        // navigate back to each element in Step 2.
        Map<String, int[]> elementTreePath = new LinkedHashMap<>();
        Map<String, CellInfo> cellInfoById = new LinkedHashMap<>();

        // Map inline Link text -> link info for OBJR association in tagLinkAnnotations.
        // Populated when P elements with Docling Link children are processed.
        Map<String, InlineLinkInfo> inlineLinkPaths = new LinkedHashMap<>();

        int sectionDepth = 0;   // logical heading level of deepest open Sect
        int sectNesting = 0;    // actual number of Sect ancestors on the pointer stack
        int tagged = 0;
        List<Integer> currentPath = new ArrayList<>();

        for (ElementInfo info : allElements) {
            boolean hasRuns = elementsWithRuns.contains(info.id);
            // hasChildContent() rescues containers whose readable text lives
            // only in children (list items, table cells, TOC items) -- but
            // only on pages where the overlay/fallback machinery is what
            // will actually fill them in. On an ordinary page, a container
            // with no runs of its own and no direct text is far more often
            // a sibling that lost its runs to a neighbouring element (LI/TD
            // bbox off by a couple points) than a genuine overlay gap; admitting
            // it here resurrects it for the LI/cell fill backstop, which then
            // writes an invisible MCR duplicating text a sibling already tags
            // (confirmed on a real document: /LI 22->31, one
            // name read twice). Keep the pre-branch behavior on ordinary
            // pages; only extend the rescue where it was designed for.
            boolean onFallbackPage = overlayPages.contains(info.pageNo)
                || formImagePages.contains(info.pageNo)
                || invisibleOnlyPages.contains(info.pageNo);
            boolean needsTag = hasRuns
                || "Figure".equals(info.tag)
                || "Formula".equals(info.tag)
                || isUsableText(info.text)
                || (onFallbackPage && hasChildContent(info));

            if (!needsTag) continue;

            // Set page for this element (also used for Sect below)
            tagPointer.setPageForTagging(
                pdfDoc.getPage(Math.min(info.pageNo, numPages)));

            // Heading hierarchy
            int headingLevel = getHeadingLevel(info.tag);
            if (headingLevel > 0) {
                while (sectionDepth >= headingLevel && sectNesting > 0) {
                    tagPointer.moveToParent();
                    currentPath.remove(currentPath.size() - 1);
                    sectionDepth--;
                    sectNesting--;
                }
                int sectIdx = tagPointer.getKidsRoles().size();
                tagPointer.addTag("Sect");
                // Set /Pg on Sect for Acrobat order panel
                PdfStructElem sectElem = tagPointer.getContext()
                    .getPointerStructElem(tagPointer);
                sectElem.put(PdfName.Pg, pdfDoc.getPage(
                    Math.min(info.pageNo, numPages)).getPdfObject());
                currentPath.add(sectIdx);
                sectionDepth = headingLevel;
                sectNesting++;
            }

            // Add the structure element
            int childIdx = tagPointer.getKidsRoles().size();
            tagPointer.addTag(info.tag);

            // Set properties
            PdfStructElem se = tagPointer.getContext()
                .getPointerStructElem(tagPointer);

            // --- Table hierarchy: create TR > TH/TD children ---
            if ("Table".equals(info.tag) && info.cells != null
                    && !info.cells.isEmpty()) {
                addTableHierarchy(tagPointer, info, currentPath, childIdx,
                    pdfDoc, numPages, elementTreePath, pageRunMap, cellInfoById);
                tagPointer.moveToParent(); // back from Table
                tagged++;
                continue;
            }

            // --- List hierarchy: create LI > Lbl + LBody children ---
            if ("L".equals(info.tag) && info.listItems != null
                    && !info.listItems.isEmpty()) {
                addListHierarchy(tagPointer, info, currentPath, childIdx,
                    pdfDoc, numPages, elementTreePath, pageRunMap);
                tagPointer.moveToParent(); // back from L
                tagged++;
                continue;
            }

            // --- TOC hierarchy: create TOCI → P children ---
            if ("TOC".equals(info.tag) && info.tocItems != null
                    && !info.tocItems.isEmpty()) {
                addTocHierarchy(tagPointer, info, currentPath, childIdx,
                    pdfDoc, numPages, elementTreePath, pageRunMap,
                    runXY, pageLinkRects);
                // Record parent TOC path so unmatched-element overlay can
                // find and iterate TOCI children for MCR fallback.
                int[] tocParentPath = new int[currentPath.size() + 1];
                for (int j = 0; j < currentPath.size(); j++)
                    tocParentPath[j] = currentPath.get(j);
                tocParentPath[currentPath.size()] = childIdx;
                elementTreePath.put(info.id, tocParentPath);
                tagPointer.moveToParent(); // back from TOC
                tagged++;
                continue;
            }

            // --- P with inline Link children OR link-covered runs: create
            //     P > Span + Link via the geometric peel.  Covers both
            //     Docling-Link anchors (paragraphChildren) and plain / Span-only
            //     paragraphs whose runs fall inside a PDF link annotation. ---
            boolean pHasChildren = info.paragraphChildren != null
                && !info.paragraphChildren.isEmpty();
            boolean pHasGeoLink = anyRunCoveredByLink(info.parentRunRefs,
                runXY, pageLinkRects);
            if ("P".equals(info.tag) && (pHasChildren || pHasGeoLink)) {
                addParagraphWithLinks(tagPointer, info, currentPath, childIdx,
                    pdfDoc, numPages, elementTreePath, pageRunMap,
                    inlineLinkPaths, hasRuns, runXY, pageLinkRects);
                tagPointer.moveToParent(); // back from P
                tagged++;
                continue;
            }

            // /ActualText is intentionally NOT set on leaf SEs (F1 fix).
            // Phase 0 finding: /ActualText blocks AROL/NVDA from reading
            // MCR content, even when an MCR is also present. Unmatched
            // elements get an invisible-text fallback MCR in STEP 2b —
            // that MCR is what AROL reads.
            if ("Figure".equals(info.tag)) {
                String figAlt = resolveFigureAlt(info);
                if (figAlt != null) {
                    se.setAlt(new PdfString(figAlt,
                        PdfEncodings.UNICODE_BIG));
                }
            }
            if ("Formula".equals(info.tag)) {
                String alt = resolveFormulaAlt(info);
                if (alt != null) {
                    se.setAlt(new PdfString(alt,
                        PdfEncodings.UNICODE_BIG));
                }
                // Additively embed MathML as an Associated File (PDF 2.0 /
                // PDF/UA-2). The /Alt caption above stays as the universal
                // fallback; this only benefits MathML-aware readers.
                embedMathmlAF(pdfDoc, se, info);
            }

            // Layout attributes: BBox + Placement (PAC Figure bounding box)
            if ("Figure".equals(info.tag) && info.hasBbox) {
                PdfDictionary layoutAttrs = new PdfDictionary();
                layoutAttrs.put(new PdfName("O"), new PdfName("Layout"));
                layoutAttrs.put(new PdfName("BBox"),
                    new PdfArray(new float[]{info.bboxL, info.bboxB,
                                              info.bboxR, info.bboxT}));
                layoutAttrs.put(new PdfName("Placement"),
                    new PdfName("Block"));
                layoutAttrs.put(new PdfName("Width"),
                    new PdfNumber(info.bboxR - info.bboxL));
                layoutAttrs.put(new PdfName("Height"),
                    new PdfNumber(info.bboxT - info.bboxB));
                se.put(PdfName.A, layoutAttrs);
            }

            // Record tree position for Step 2 navigation.
            // Include elements with runs, Figures, and unmatched text
            // (unmatched elements get empty MCR in Step 2b).
            if (hasRuns || "Figure".equals(info.tag)
                    || isUsableText(info.text)) {
                int[] path = new int[currentPath.size() + 1];
                for (int j = 0; j < currentPath.size(); j++)
                    path[j] = currentPath.get(j);
                path[currentPath.size()] = childIdx;
                elementTreePath.put(info.id, path);
            }

            tagPointer.moveToParent();
            tagged++;
        }

        // Close remaining sections
        while (sectNesting > 0) {
            tagPointer.moveToParent();
            sectNesting--;
        }
        sectionDepth = 0;

        System.out.println("  Structure tree: " + tagged + " elements created");

        // ============================================================
        // STEP 2: Edit content streams with BDC/EMC using TagReferences
        // ============================================================

        // Load embedded font for overlay fallback and unmatched MCR fill
        PdfFont overlayFont = null;
        try {
            overlayFont = PdfAccessibilityTagger.loadEmbeddedFont();
            if (!overlayPages.isEmpty()) {
                System.out.println("  Overlay font loaded for fallback pages");
            }
        } catch (Exception e) {
            System.err.println("  WARNING: Could not load overlay font: "
                + e.getMessage());
        }

        // Track elements processed via overlay (excluded from STEP 2b)
        Set<String> overlayProcessedElems = new HashSet<>();

        // B2: track which elements emitted at least one real MCID across all
        // pages.  Elements in elementsWithRuns whose runs all got artifact-
        // wrapped (e.g. whitespace-only) won't appear here and must be
        // recovered by Step 2b.
        Set<String> taggedAcrossPages = new HashSet<>();

        // Document-scoped visited sets for the four XObject sweeps below.
        // Per-page sets made every page re-decompress and re-sweep each
        // SHARED Form XObject once per referencing page (the sweeps are
        // idempotent and page-independent, so only the first visit does
        // real work) — on page-shared multi-MB forms that was the same
        // cost class as the 21MB-form OOM.
        Set<PdfIndirectReference> invisSweepVisited = new HashSet<>();
        Set<PdfIndirectReference> mcSweepVisited = new HashSet<>();
        Set<PdfIndirectReference> notdefSweepVisited = new HashSet<>();
        Set<PdfIndirectReference> inlineImgSweepVisited = new HashSet<>();

        for (int p = 1; p <= numPages; p++) {
            PdfPage page = pdfDoc.getPage(p);

            // Pre-process content stream before editing.
            //
            // Invisible-only pages (Tr=3 OCR text): remap Tr=3→Tr=0 white
            // instead of stripping, so the editor can wrap the OCR text in
            // BDC/EMC structure tags.  Operator indices stay aligned with
            // the analyzer's raw-content re-analysis.
            //
            // Other pages: strip invisible text + hidden OCR fonts + .notdef
            // glyphs as before (the OCR overlay is redundant on pages with
            // visible text).
            try {
                byte[] pageBytes = page.getContentBytes();
                if (pageBytes != null && pageBytes.length > 0) {
                    byte[] cleaned;
                    PdfDictionary pageRes = page.getResources().getPdfObject();

                    if (invisibleOnlyPages.contains(p)) {
                        // Normalize operator spacing: iText 9's tokenizer
                        // merges "Tf( )Tj" into unknown keyword "TfTj".
                        // Must run first for correct font tracking downstream.
                        cleaned = PdfAccessibilityTagger
                            .normalizeOperatorSpacing(pageBytes);
                        // Keep Tr=3 OCR text as-is for content-preserving
                        // tagging.  The editor wraps text operators with
                        // MCIDs regardless of rendering mode.  /ActualText
                        // on structure elements provides screen reader
                        // content.  Acrobat's own OCR uses Tr=3 tagged
                        // text the same way.
                        // Still strip hidden OCR fonts (.notdef producers)
                        Set<String> hiddenFonts = PdfAccessibilityTagger
                            .findHiddenOCRFontNames(pageRes);
                        if (!hiddenFonts.isEmpty()) {
                            cleaned = PdfAccessibilityTagger
                                .stripFontText(cleaned, hiddenFonts);
                        }
                        // Strip marked content from source (avoid MCID conflicts)
                        cleaned = PdfAccessibilityTagger
                            .stripMarkedContent(cleaned);
                        // Strip inline images (BI...ID...EI) to prevent
                        // iText PdfCanvasEditor corruption
                        cleaned = PdfAccessibilityTagger
                            .stripInlineImages(cleaned);
                    } else {
                        // Normalize operator spacing FIRST: ensures each
                        // operator is properly separated, which is required
                        // for correct font tracking in stripNotdefGlyphs
                        // (processes line-by-line) and for iText's tokenizer
                        // (fails to split "Tf( )Tj" into separate operators).
                        cleaned = PdfAccessibilityTagger
                            .normalizeOperatorSpacing(pageBytes);
                        // Normal path: strip invisible text entirely
                        cleaned = PdfAccessibilityTagger
                            .stripInvisibleText(cleaned);
                        Set<String> hiddenFonts = PdfAccessibilityTagger
                            .findHiddenOCRFontNames(pageRes);
                        if (!hiddenFonts.isEmpty()) {
                            cleaned = PdfAccessibilityTagger
                                .stripFontText(cleaned, hiddenFonts);
                        }
                        // Fix font inheritance first, then strip .notdef.
                        cleaned = PdfAccessibilityTagger
                            .fixFontInheritance(cleaned);
                        cleaned = PdfAccessibilityTagger
                            .stripNotdefGlyphs(cleaned, pageRes);
                        // Strip inline images (BI...ID...EI) to prevent
                        // iText PdfCanvasEditor corruption
                        cleaned = PdfAccessibilityTagger
                            .stripInlineImages(cleaned);
                    }

                    if (!java.util.Arrays.equals(cleaned, pageBytes)) {
                        // Replace /Contents with a fresh, non-shared stream.
                        // Do NOT setData() into getContentStream(0): some
                        // producers reuse one stream object as the first
                        // /Contents entry of every page (a " q " prologue),
                        // and writing this page's content into it poisons all
                        // the other pages that share it (cross-page content
                        // duplication + NPE on colorspaces those pages don't
                        // declare). See replacePageContents().
                        replacePageContents(pdfDoc, page, cleaned);
                        PdfAccessibilityTagger.sqInvisibleTextPagesStripped++;
                        System.err.println("  Page " + p
                            + (invisibleOnlyPages.contains(p)
                                ? ": preprocessed Tr=3 page ("
                                : ": stripped invisible text (")
                            + pageBytes.length + " -> " + cleaned.length
                            + " bytes)");
                    }
                    // Strip from Form XObjects (both paths)
                    PdfAccessibilityTagger.stripInvisibleTextFromXObjects(
                        pageRes, invisSweepVisited);
                    PdfAccessibilityTagger.stripMarkedContentFromXObjects(
                        pageRes, mcSweepVisited);
                    PdfAccessibilityTagger.stripNotdefGlyphsFromXObjects(
                        pageRes, notdefSweepVisited);
                    PdfAccessibilityTagger.stripInlineImagesFromXObjects(
                        pageRes, inlineImgSweepVisited);
                }
            } catch (Exception e) {
                System.err.println("  Page " + p
                    + ": invisible text stripping failed: " + e.getMessage());
            }

            Map<Integer, ElementInfo> runMap =
                pageRunMap.getOrDefault(p, Collections.emptyMap());
            // On invisible-only pages, the full-page scanned image is
            // background (artifact), not a semantic Figure.  Skipping
            // Figure tagging prevents breaking the q/Q graphics state
            // nesting with Artifact/Figure/Artifact marked content splits.
            Map<Integer, ElementInfo> imageMap;
            if (invisibleOnlyPages.contains(p)) {
                imageMap = Collections.emptyMap();
                Map<Integer, ElementInfo> origImageMap =
                    pageImageMap.getOrDefault(p, Collections.emptyMap());
                if (!origImageMap.isEmpty()) {
                    System.out.println("  Page " + p
                        + ": skipped Figure tagging for "
                        + origImageMap.size()
                        + " image(s) on invisible-only page");
                }
            } else {
                imageMap = pageImageMap.getOrDefault(p, Collections.emptyMap());
            }
            Map<Integer, ElementInfo> xobjectMap =
                pageXobjectMap.getOrDefault(p, Collections.emptyMap());

            // Form-image pages: image-based fillable forms where content stream
            // has no/trivial text.  Keep form background visible as Figure,
            // write MCRs for widget labels.  Checked BEFORE runMap/overlay
            // logic since these pages may have a few stray runs (e.g. spaces).
            if (formImagePages.contains(p) && overlayFont != null) {
                int fipCount = FormImagePageTagger.tagFormImagePage(
                    pdfDoc, page, p, tagPointer, allElements,
                    elementTreePath, overlayFont, overlayProcessedElems);
                System.out.println("  Page " + p
                    + ": form-image page (" + fipCount + " elements)");
                continue;
            }

            if (runMap.isEmpty() && imageMap.isEmpty()
                    && xobjectMap.isEmpty()) {
                if (overlayPages.contains(p) && overlayFont != null) {
                    // Scanned/image-only page with Docling OCR elements:
                    // wrap visible content as Artifact, then write overlay MCR
                    wrapPageAsArtifact(pdfDoc, page);

                    // Ensure StructParents for MCR registration
                    if (page.getPdfObject().get(new PdfName("StructParents")) == null) {
                        page.getPdfObject().put(new PdfName("StructParents"),
                            new PdfNumber(pdfDoc.getNextStructParentIndex()));
                    }
                    PdfCanvas overlayCanvas = getAppendCanvas(page, pdfDoc);

                    // Read page rotation for coordinate transform
                    int ovlRotation = 0;
                    PdfNumber ovlRotObj = page.getPdfObject()
                        .getAsNumber(PdfName.Rotate);
                    if (ovlRotObj != null) ovlRotation = ovlRotObj.intValue();
                    float ovlMbW = page.getMediaBox().getWidth();
                    float ovlMbH = page.getMediaBox().getHeight();

                    int overlayCount = 0;
                    for (ElementInfo ei : allElements) {
                        if (ei.pageNo != p) continue;
                        int[] path = elementTreePath.get(ei.id);
                        if (path == null) {
                            if ("L".equals(ei.tag) || "Table".equals(ei.tag)
                                    || "TOC".equals(ei.tag)) {
                                // fall through for container elements
                            } else {
                                continue;
                            }
                        }

                        // For Tables, write each cell
                        if ("Table".equals(ei.tag) && ei.cells != null
                                && !ei.cells.isEmpty()) {
                            for (CellInfo ci : ei.cells) {
                                int[] cellPath = elementTreePath.get(ci.cellId);
                                if (cellPath == null) continue;
                                String cellText = ci.text;
                                if (cellText == null || cellText.trim().isEmpty())
                                    continue;
                                float cx = ci.hasBbox ? ci.bboxL : (ei.hasBbox ? ei.bboxL : 72);
                                float cy = ci.hasBbox ? ci.bboxB : (ei.hasBbox ? ei.bboxB : 72);
                                float cxR = ci.hasBbox ? ci.bboxR : cx + 50;
                                float cyT = ci.hasBbox ? ci.bboxT : cy + 12;
                                try {
                                    tagPointer.moveToRoot();
                                    for (int idx : cellPath)
                                        tagPointer.moveToKid(idx);
                                    tagPointer.setPageForTagging(page);
                                    PdfAccessibilityTagger.writeDistributedMCR(
                                        overlayCanvas, tagPointer, overlayFont,
                                        cellText, cx, cy, cxR, cyT,
                                        ovlRotation, ovlMbW, ovlMbH);
                                    overlayProcessedElems.add(ci.cellId);
                                    overlayCount++;
                                    tagPointer.moveToRoot();
                                } catch (Exception e) {
                                    try { tagPointer.moveToRoot(); }
                                    catch (Exception ignored) {}
                                }
                            }
                            overlayProcessedElems.add(ei.id);
                            continue;
                        }

                        // For Lists, write each LI
                        if ("L".equals(ei.tag) && ei.listItems != null
                                && !ei.listItems.isEmpty()) {
                            for (ListItemInfo li : ei.listItems) {
                                int[] liPath = elementTreePath.get(li.liId);
                                if (liPath == null) continue;
                                String liText = li.text;
                                if (liText == null || liText.trim().isEmpty())
                                    continue;
                                float lx = li.hasBbox ? li.bboxL : (ei.hasBbox ? ei.bboxL : 72);
                                float ly = li.hasBbox ? li.bboxB : (ei.hasBbox ? ei.bboxB : 72);
                                float lxR = li.hasBbox ? li.bboxR : lx + 200;
                                float lyT = li.hasBbox ? li.bboxT : ly + 12;
                                try {
                                    tagPointer.moveToRoot();
                                    for (int idx : liPath)
                                        tagPointer.moveToKid(idx);
                                    tagPointer.setPageForTagging(page);
                                    PdfAccessibilityTagger.writeDistributedMCR(
                                        overlayCanvas, tagPointer, overlayFont,
                                        liText, lx, ly, lxR, lyT,
                                        ovlRotation, ovlMbW, ovlMbH);
                                    overlayProcessedElems.add(li.liId);
                                    overlayCount++;
                                    tagPointer.moveToRoot();
                                } catch (Exception e) {
                                    try { tagPointer.moveToRoot(); }
                                    catch (Exception ignored) {}
                                }
                            }
                            overlayProcessedElems.add(ei.id);
                            continue;
                        }

                        // For TOC, write each TOCI
                        if ("TOC".equals(ei.tag) && ei.tocItems != null
                                && !ei.tocItems.isEmpty()) {
                            for (TocItemInfo ti : ei.tocItems) {
                                int[] tiPath = elementTreePath.get(ti.tociId);
                                if (tiPath == null) continue;
                                String tiText = ti.text;
                                if (tiText == null || tiText.trim().isEmpty())
                                    continue;
                                float tx = ei.hasBbox ? ei.bboxL : 72;
                                float ty = ei.hasBbox ? ei.bboxB : 72;
                                float txR = ei.hasBbox ? ei.bboxR : tx + 200;
                                float tyT = ei.hasBbox ? ei.bboxT : ty + 12;
                                try {
                                    tagPointer.moveToRoot();
                                    for (int idx : tiPath)
                                        tagPointer.moveToKid(idx);
                                    tagPointer.setPageForTagging(page);
                                    PdfAccessibilityTagger.writeDistributedMCR(
                                        overlayCanvas, tagPointer, overlayFont,
                                        tiText, tx, ty, txR, tyT,
                                        ovlRotation, ovlMbW, ovlMbH);
                                    overlayProcessedElems.add(ti.tociId);
                                    overlayCount++;
                                    tagPointer.moveToRoot();
                                } catch (Exception e) {
                                    try { tagPointer.moveToRoot(); }
                                    catch (Exception ignored) {}
                                }
                            }
                            overlayProcessedElems.add(ei.id);
                            continue;
                        }

                        // Regular elements (P, H1, Figure, etc.)
                        String mcrText = (ei.text != null
                                && !ei.text.trim().isEmpty())
                            ? ei.text : ei.altText;
                        if (mcrText == null || mcrText.trim().isEmpty())
                            continue;
                        try {
                            tagPointer.moveToRoot();
                            for (int idx : path)
                                tagPointer.moveToKid(idx);
                            tagPointer.setPageForTagging(page);
                            if (ei.hasBbox) {
                                PdfAccessibilityTagger.writeDistributedMCR(
                                    overlayCanvas, tagPointer, overlayFont,
                                    mcrText, ei.bboxL, ei.bboxB,
                                    ei.bboxR, ei.bboxT,
                                    ovlRotation, ovlMbW, ovlMbH);
                            } else {
                                float[] rcs = FormImagePageTagger.displayToContent(
                                    72, 72, ovlRotation, ovlMbW, ovlMbH);
                                PdfAccessibilityTagger.writeInvisibleMCR(
                                    overlayCanvas, tagPointer, overlayFont,
                                    rcs[0], rcs[1], mcrText);
                            }
                            overlayProcessedElems.add(ei.id);
                            overlayCount++;
                            tagPointer.moveToRoot();
                        } catch (Exception e) {
                            try { tagPointer.moveToRoot(); }
                            catch (Exception ignored) {}
                        }
                    }
                    System.out.println("  Page " + p
                        + ": overlay fallback (" + overlayCount
                        + " elements, distributed)");
                    continue;
                }
                wrapPageAsArtifact(pdfDoc, page);
                System.out.println("  Page " + p + ": wrapped as Artifact");
                continue;
            }

            // Build image TagReferences (still pre-allocated — images
            // don't have font-switching issues)
            Map<Integer, TagReference> imageToTagRef = new HashMap<>();
            for (var entry : imageMap.entrySet()) {
                int[] path = elementTreePath.get(entry.getValue().id);
                if (path == null) continue;
                tagPointer.moveToRoot();
                try {
                    for (int idx : path) {
                        tagPointer.moveToKid(idx);
                    }
                    tagPointer.setPageForTagging(page);
                    imageToTagRef.put(entry.getKey(),
                        tagPointer.getTagReference());
                    tagPointer.moveToRoot();
                } catch (Exception e) {
                    tagPointer.moveToRoot();
                }
            }

            // Build Form XObject TagReferences (for 7.20 t2 compliance)
            Map<Integer, TagReference> xobjectToTagRef = new HashMap<>();
            for (var entry : xobjectMap.entrySet()) {
                int[] path = elementTreePath.get(entry.getValue().id);
                if (path == null) continue;
                tagPointer.moveToRoot();
                try {
                    for (int idx : path) {
                        tagPointer.moveToKid(idx);
                    }
                    tagPointer.setPageForTagging(page);
                    xobjectToTagRef.put(entry.getKey(),
                        tagPointer.getTagReference());
                    tagPointer.moveToRoot();
                } catch (Exception e) {
                    tagPointer.moveToRoot();
                }
            }

            // Ensure StructParents
            if (page.getPdfObject().get(new PdfName("StructParents")) == null) {
                page.getPdfObject().put(new PdfName("StructParents"),
                    new PdfNumber(pdfDoc.getNextStructParentIndex()));
            }

            // Detect inline images (BI...ID...EI) in the content stream.
            // iText's PdfCanvasEditor re-serializes them as <<...>>stream
            // ...endstream EI which is invalid PDF syntax, causing MuPDF
            // "syntax error in content stream" on every page.  Fall back to
            // overlay mode for these pages (extremely rare: ~1/1000 PDFs).
            boolean hasInlineImages = false;
            try {
                byte[] contentCheck = page.getContentBytes();
                if (contentCheck != null) {
                    String cs = new String(contentCheck,
                        java.nio.charset.StandardCharsets.ISO_8859_1);
                    // Match BI operator at line start or after line
                    // break, followed by whitespace.  Handles LF, CRLF,
                    // and space-delimited variants ("\r\nBI /W 1 ...").
                    hasInlineImages = java.util.regex.Pattern.compile(
                        "(?:^|\\r?\\n)BI(?:\\s|\\r?\\n)")
                        .matcher(cs).find();
                }
            } catch (Exception ignored) {}

            if (hasInlineImages && overlayFont != null) {
                System.out.println("  Page " + p
                    + ": inline images detected — using overlay fallback");
                wrapPageAsArtifact(pdfDoc, page);
                if (page.getPdfObject().get(new PdfName("StructParents")) == null) {
                    page.getPdfObject().put(new PdfName("StructParents"),
                        new PdfNumber(pdfDoc.getNextStructParentIndex()));
                }
                PdfCanvas overlayCanvas = getAppendCanvas(page, pdfDoc);
                int overlayCount = 0;
                for (ElementInfo ei : allElements) {
                    if (ei.pageNo != p) continue;
                    int[] path = elementTreePath.get(ei.id);
                    if (path == null) continue;
                    if ("Table".equals(ei.tag) && ei.cells != null
                            && !ei.cells.isEmpty()) {
                        for (CellInfo ci : ei.cells) {
                            int[] cellPath = elementTreePath.get(ci.cellId);
                            if (cellPath == null) continue;
                            String cellText = ci.text;
                            if (cellText == null || cellText.trim().isEmpty())
                                continue;
                            try {
                                tagPointer.moveToRoot();
                                for (int idx : cellPath)
                                    tagPointer.moveToKid(idx);
                                tagPointer.setPageForTagging(page);
                                PdfAccessibilityTagger.writeInvisibleMCR(
                                    overlayCanvas, tagPointer, overlayFont,
                                    ei.hasBbox ? ei.bboxL : 72,
                                    ei.hasBbox ? ei.bboxB : 72,
                                    cellText);
                                overlayCount++;
                                tagPointer.moveToRoot();
                            } catch (Exception e) {
                                try { tagPointer.moveToRoot(); }
                                catch (Exception ignored2) {}
                            }
                        }
                        continue;
                    }
                    if ("L".equals(ei.tag) && ei.listItems != null
                            && !ei.listItems.isEmpty()) {
                        for (ListItemInfo li : ei.listItems) {
                            int[] liPath = elementTreePath.get(li.liId);
                            if (liPath == null) continue;
                            String liText = li.text;
                            if (liText == null || liText.trim().isEmpty())
                                continue;
                            try {
                                tagPointer.moveToRoot();
                                for (int idx : liPath)
                                    tagPointer.moveToKid(idx);
                                tagPointer.setPageForTagging(page);
                                PdfAccessibilityTagger.writeInvisibleMCR(
                                    overlayCanvas, tagPointer, overlayFont,
                                    ei.hasBbox ? ei.bboxL : 72,
                                    ei.hasBbox ? ei.bboxB : 72,
                                    liText);
                                overlayCount++;
                                tagPointer.moveToRoot();
                            } catch (Exception e) {
                                try { tagPointer.moveToRoot(); }
                                catch (Exception ignored2) {}
                            }
                        }
                        continue;
                    }
                    if ("TOC".equals(ei.tag) && ei.tocItems != null
                            && !ei.tocItems.isEmpty()) {
                        for (TocItemInfo ti : ei.tocItems) {
                            int[] tiPath = elementTreePath.get(ti.tociId);
                            if (tiPath == null) continue;
                            String tiText = ti.text;
                            if (tiText == null || tiText.trim().isEmpty())
                                continue;
                            try {
                                tagPointer.moveToRoot();
                                for (int idx : tiPath)
                                    tagPointer.moveToKid(idx);
                                tagPointer.setPageForTagging(page);
                                PdfAccessibilityTagger.writeInvisibleMCR(
                                    overlayCanvas, tagPointer, overlayFont,
                                    ei.hasBbox ? ei.bboxL : 72,
                                    ei.hasBbox ? ei.bboxB : 72,
                                    tiText);
                                overlayCount++;
                                tagPointer.moveToRoot();
                            } catch (Exception e) {
                                try { tagPointer.moveToRoot(); }
                                catch (Exception ignored2) {}
                            }
                        }
                        continue;
                    }
                    String mcrText = (ei.text != null
                            && !ei.text.trim().isEmpty())
                        ? ei.text : ei.altText;
                    if (mcrText == null || mcrText.trim().isEmpty())
                        continue;
                    try {
                        tagPointer.moveToRoot();
                        for (int idx : path)
                            tagPointer.moveToKid(idx);
                        tagPointer.setPageForTagging(page);
                        PdfAccessibilityTagger.writeInvisibleMCR(
                            overlayCanvas, tagPointer, overlayFont,
                            ei.hasBbox ? ei.bboxL : 72,
                            ei.hasBbox ? ei.bboxB : 72,
                            mcrText);
                        overlayCount++;
                        tagPointer.moveToRoot();
                    } catch (Exception e) {
                        try { tagPointer.moveToRoot(); }
                        catch (Exception ignored2) {}
                    }
                }
                System.out.println("  Page " + p
                    + ": overlay fallback (" + overlayCount
                    + " elements, inline images preserved)");
                continue;
            }

            // Edit content stream — each text run gets its own BDC/EMC
            // to avoid font-switching issues in long marked content blocks
            ContentPreservingEditor editor = new ContentPreservingEditor(
                runMap, imageToTagRef, xobjectToTagRef,
                tagPointer, elementTreePath, page,
                invisibleOnlyPages.contains(p));
            // Pre-scan the content bytes that the editor will process to
            // capture raw PdfString glyph IDs before iText's parser decodes
            // them through font encoding.  Must use page.getContentBytes()
            // which reflects any invisible text stripping already applied.
            // Apply the same operator spacing normalization so prescan and
            // editor see identical token sequences.
            editor.prescanRawTextBytes(page.getContentBytes());
            editor.editPage(pdfDoc, p, true);
            taggedAcrossPages.addAll(editor.taggedElementIds);

            // Post-editor .notdef strip: iText may re-encode literal
            // strings (e.g. raw 0x01 → \001), so re-run stripNotdefGlyphs
            // on the editor's output to catch any surviving .notdef chars.
            try {
                byte[] editedBytes = page.getContentBytes();
                PdfDictionary postRes = page.getResources().getPdfObject();
                byte[] postCleaned = PdfAccessibilityTagger
                    .stripNotdefGlyphs(editedBytes, postRes);
                if (postCleaned != editedBytes) {
                    // editPage() above already replaced /Contents with a fresh
                    // stream, so getContentStream(0) is page-private here — but
                    // route through the helper anyway for consistency and to stay
                    // shared-stream-safe if the call order ever changes.
                    replacePageContents(pdfDoc, page, postCleaned);
                }
            } catch (Exception ignored) {}

            System.out.println("  Page " + p + ": " + editor.taggedCount
                + " tagged, " + editor.artifactCount + " artifacts"
                + " (textRunIndex=" + editor.textRunIndex
                + ", runMapSize=" + editor.totalRunMapEntries
                + ", maxRunIdx=" + editor.maxRunIndex + ")");

            String _invisWarn = forceInvisibleWarning(
                p, editor.forceInvisibleFailures);
            if (_invisWarn != null) {
                System.err.println(_invisWarn);
            }
            if (editor.textStateReplayFailures > 0) {
                System.err.println("  WARNING: Page " + p
                    + ": failed to replay buffered text-state ops in "
                    + editor.textStateReplayFailures
                    + " marked-content scope(s) — text may be painted at "
                    + "the wrong position, size or font");
            }
        }

        // ============================================================
        // STEP 2b: Empty MCR for unmatched elements (no content runs)
        // ============================================================
        // Elements with tree paths but no content runs: write invisible
        // MCR text (same as overlay fallback) when element has text,
        // or empty BDC/EMC for elements without text (Figures, containers).
        int filledMcrCount = 0;
        int emptyMcrCount = 0;
        // Collect all elements that were processed via content runs
        Set<String> processedElems = new HashSet<>();
        for (int p = 1; p <= numPages; p++) {
            Map<Integer, ElementInfo> rm = pageRunMap.getOrDefault(p,
                Collections.emptyMap());
            Map<Integer, ElementInfo> im = pageImageMap.getOrDefault(p,
                Collections.emptyMap());
            for (var e : rm.values()) processedElems.add(e.id);
            for (var e : im.values()) processedElems.add(e.id);
        }
        // Table/L elements whose cells/LIs have content runs are already
        // tagged via those children — don't re-process as unmatched.
        processedElems.addAll(elementsWithRuns);

        // B2: elements in elementsWithRuns whose runs all got artifact-wrapped
        // (e.g. whitespace-only after fix #35) never emitted any MCID. Remove
        // them from processedElems so Step 2b's element-level fill catches them.
        Set<String> silentFailures = new HashSet<>(elementsWithRuns);
        silentFailures.removeAll(taggedAcrossPages);
        processedElems.removeAll(silentFailures);

        // Build elementById lookup for text/bbox access
        Map<String, ElementInfo> elementById = new HashMap<>();
        for (ElementInfo ei : allElements) {
            elementById.put(ei.id, ei);
        }

        // Group unprocessed elements by page
        Map<Integer, List<String>> unmatchedByPage = new TreeMap<>();
        for (ElementInfo info : allElements) {
            if (processedElems.contains(info.id)) continue;
            if (overlayProcessedElems.contains(info.id)) continue;
            if (!elementTreePath.containsKey(info.id)) continue;
            unmatchedByPage.computeIfAbsent(info.pageNo, k -> new ArrayList<>())
                .add(info.id);
        }

        for (var entry : unmatchedByPage.entrySet()) {
            int p = entry.getKey();
            if (p < 1 || p > numPages) continue;
            PdfPage page = pdfDoc.getPage(p);

            // Ensure StructParents (may not have been set if page had
            // no content runs but has unmatched elements)
            if (page.getPdfObject().get(new PdfName("StructParents")) == null) {
                page.getPdfObject().put(new PdfName("StructParents"),
                    new PdfNumber(pdfDoc.getNextStructParentIndex()));
            }

            for (String elemId : entry.getValue()) {
                int[] path = elementTreePath.get(elemId);
                if (path == null) continue;
                ElementInfo ei = elementById.get(elemId);

                // L (list) content is handled per-LI by the list-item fill
                // pass below (which also covers partially-matched lists); skip
                // here so the generic branch doesn't write a redundant
                // list-level MCR.
                if (ei != null && "L".equals(ei.tag)) continue;

                // TOC: write each TOCI child as separate overlay MCR
                if (ei != null && "TOC".equals(ei.tag)
                        && ei.tocItems != null && !ei.tocItems.isEmpty()
                        && overlayFont != null) {
                    for (TocItemInfo ti : ei.tocItems) {
                        int[] tiPath = elementTreePath.get(ti.tociId);
                        if (tiPath == null) continue;
                        String tiText = ti.text;
                        if (tiText == null || tiText.trim().isEmpty())
                            continue;
                        try {
                            tagPointer.moveToRoot();
                            for (int idx : tiPath)
                                tagPointer.moveToKid(idx);
                            tagPointer.setPageForTagging(page);
                            PdfCanvas canvas = getAppendCanvas(page, pdfDoc);
                            PdfAccessibilityTagger.writeInvisibleMCR(
                                canvas, tagPointer, overlayFont,
                                ei.hasBbox ? ei.bboxL : 72,
                                ei.hasBbox ? ei.bboxB : 72,
                                tiText);
                            filledMcrCount++;
                            tagPointer.moveToRoot();
                        } catch (Exception e) {
                            try { tagPointer.moveToRoot(); }
                            catch (Exception ignored) {}
                        }
                    }
                    continue;
                }

                String text = (ei != null && ei.text != null)
                    ? ei.text.trim() : "";
                try {
                    tagPointer.moveToRoot();
                    for (int idx : path) {
                        tagPointer.moveToKid(idx);
                    }
                    tagPointer.setPageForTagging(page);
                    if (!text.isEmpty() && overlayFont != null) {
                        PdfCanvas canvas = getAppendCanvas(page, pdfDoc);
                        if (overlayPages.contains(p)
                                && ei != null && ei.hasBbox) {
                            int ovlRot = 0;
                            PdfNumber rotN = page.getPdfObject()
                                .getAsNumber(PdfName.Rotate);
                            if (rotN != null) ovlRot = rotN.intValue();
                            PdfAccessibilityTagger.writeDistributedMCR(
                                canvas, tagPointer, overlayFont, text,
                                ei.bboxL, ei.bboxB, ei.bboxR, ei.bboxT,
                                ovlRot, page.getMediaBox().getWidth(),
                                page.getMediaBox().getHeight());
                        } else {
                            PdfAccessibilityTagger.writeInvisibleMCR(
                                canvas, tagPointer, overlayFont,
                                (ei != null && ei.hasBbox) ? ei.bboxL : 72,
                                (ei != null && ei.hasBbox) ? ei.bboxB : 72,
                                text);
                        }
                        filledMcrCount++;
                    } else {
                        // Empty MCR for elements without text
                        TagReference ref = tagPointer.getTagReference();
                        PdfCanvas emptyCanvas = getAppendCanvas(page, pdfDoc);
                        emptyCanvas.openTag(ref);
                        emptyCanvas.closeTag();
                        emptyMcrCount++;
                    }
                    tagPointer.moveToRoot();
                } catch (Exception e) {
                    System.err.println("  Warning: MCR failed for "
                        + elemId + ": " + e.getMessage());
                    try { tagPointer.moveToRoot(); }
                    catch (Exception ignored) {}
                }
            }
        }
        // Cell-level unmatched fill: cells aren't in allElements, but their
        // tree paths are registered in elementTreePath via addTableRows().
        // Cells whose runRefs were non-empty are already covered by Step 2 (they
        // have a synthetic ElementInfo in pageRunMap). Cells with empty runRefs
        // need an invisible MCR (if cell text is available) or empty BDC/EMC
        // so the SE is no longer a blank leaf.
        for (var cellEntry : cellInfoById.entrySet()) {
            String cellId = cellEntry.getKey();
            CellInfo ci = cellEntry.getValue();
            // Skip cells that successfully emitted at least one MCID via Step 2.
            // Cells with empty runRefs OR cells whose runRefs were silently
            // stolen (e.g. overlapping rowspan cells where another cell wrote
            // the same pageRunMap entries last) both need the fallback.
            if (taggedAcrossPages.contains(cellId)) continue;
            // Overlay pages already wrote this cell's MCR in the overlay
            // branch (pre-existing x2 cell text bug on scanned tables).
            if (overlayProcessedElems.contains(cellId)) continue;
            int[] path = elementTreePath.get(cellId);
            if (path == null) continue;

            int p = ci.pageNo;
            if (p < 1 || p > numPages) continue;
            PdfPage cellPage = pdfDoc.getPage(p);

            if (cellPage.getPdfObject().get(new PdfName("StructParents")) == null) {
                cellPage.getPdfObject().put(new PdfName("StructParents"),
                    new PdfNumber(pdfDoc.getNextStructParentIndex()));
            }

            try {
                tagPointer.moveToRoot();
                for (int idx : path) tagPointer.moveToKid(idx);
                tagPointer.setPageForTagging(cellPage);
                PdfCanvas canvas = getAppendCanvas(cellPage, pdfDoc);

                String cellText = ci.text != null ? ci.text.trim() : "";
                float cellX = ci.hasBbox ? ci.bboxL : 72;
                float cellY = ci.hasBbox ? ci.bboxB : 72;

                if (!cellText.isEmpty() && overlayFont != null) {
                    PdfAccessibilityTagger.writeInvisibleMCR(
                        canvas, tagPointer, overlayFont,
                        cellX, cellY, cellText);
                    filledMcrCount++;
                } else {
                    TagReference ref = tagPointer.getTagReference();
                    canvas.openTag(ref);
                    canvas.closeTag();
                    emptyMcrCount++;
                }
                tagPointer.moveToRoot();
            } catch (Exception e) {
                System.err.println("  Warning: cell MCR fallback failed for "
                    + cellId + ": " + e.getMessage());
                try { tagPointer.moveToRoot(); } catch (Exception ignored) {}
            }
        }

        // List-item unmatched fill (SR Group B2): an LI whose runRefs are empty
        // got no matched MCR.  Its parent L may be only partially matched, so it
        // never reaches the unmatchedByPage pass above.  Without an MCR the
        // LBody/Lbl would rely on /ActualText, which blinds AROL/NVDA.  Write an
        // invisible (Tr=3) MCR for each unmatched LI's Lbl + LBody via the tree
        // paths recorded in addListHierarchy.
        if (overlayFont != null) {
            for (ElementInfo ei : allElements) {
                if (!"L".equals(ei.tag) || ei.listItems == null) continue;
                int liPage = Math.min(Math.max(ei.pageNo, 1), numPages);
                PdfPage lpage = pdfDoc.getPage(liPage);
                if (lpage.getPdfObject().get(new PdfName("StructParents")) == null) {
                    lpage.getPdfObject().put(new PdfName("StructParents"),
                        new PdfNumber(pdfDoc.getNextStructParentIndex()));
                }
                for (ListItemInfo li : ei.listItems) {
                    if (li.runRefs != null && !li.runRefs.isEmpty()) continue;
                    // Overlay pages already wrote this LI's MCR in the
                    // overlay-fallback branch — writing again would double
                    // the extracted text.
                    if (overlayProcessedElems.contains(li.liId)) continue;
                    float liX = li.hasBbox ? li.bboxL : (ei.hasBbox ? ei.bboxL : 72);
                    float liY = li.hasBbox ? li.bboxB : (ei.hasBbox ? ei.bboxB : 72);
                    String[][] leaves = {
                        {li.liId + "::lbl", li.label},
                        {li.liId, li.text},
                    };
                    for (String[] leaf : leaves) {
                        int[] lpath = elementTreePath.get(leaf[0]);
                        if (lpath == null || leaf[1] == null
                                || leaf[1].trim().isEmpty()) continue;
                        try {
                            tagPointer.moveToRoot();
                            for (int idx : lpath) tagPointer.moveToKid(idx);
                            tagPointer.setPageForTagging(lpage);
                            PdfCanvas canvas = getAppendCanvas(lpage, pdfDoc);
                            PdfAccessibilityTagger.writeInvisibleMCR(
                                canvas, tagPointer, overlayFont, liX, liY, leaf[1]);
                            filledMcrCount++;
                            tagPointer.moveToRoot();
                        } catch (Exception e) {
                            try { tagPointer.moveToRoot(); }
                            catch (Exception ignored) {}
                        }
                    }
                }
            }
        }

        // Paragraph-children unmatched fill (SR Group B2 — bibliography): when a
        // P with Span/Link children had no matched runs, addParagraphWithLinks
        // recorded each child's path under "<pId>::pc<idx>".  Write an invisible
        // (Tr=3) MCR per such child so its text is AROL-readable without
        // /ActualText.  Iterating allElements (not unmatchedByPage) covers P's
        // that run-remapping marked processed.
        if (overlayFont != null) {
            for (ElementInfo ei : allElements) {
                if (ei.paragraphChildren == null
                        || ei.paragraphChildren.isEmpty()) continue;
                int pPage = Math.min(Math.max(ei.pageNo, 1), numPages);
                PdfPage ppage = pdfDoc.getPage(pPage);
                if (ppage.getPdfObject().get(new PdfName("StructParents")) == null) {
                    ppage.getPdfObject().put(new PdfName("StructParents"),
                        new PdfNumber(pdfDoc.getNextStructParentIndex()));
                }
                float px = ei.hasBbox ? ei.bboxL : 72;
                float py = ei.hasBbox ? ei.bboxB : 72;
                for (int ci = 0; ci < ei.paragraphChildren.size(); ci++) {
                    int[] cpath = elementTreePath.get(ei.id + "::pc" + ci);
                    if (cpath == null) continue; // matched (or parent had runs)
                    ParagraphChildInfo child = ei.paragraphChildren.get(ci);
                    if (child.text == null || child.text.trim().isEmpty()) continue;
                    try {
                        tagPointer.moveToRoot();
                        for (int idx : cpath) tagPointer.moveToKid(idx);
                        tagPointer.setPageForTagging(ppage);
                        PdfCanvas canvas = getAppendCanvas(ppage, pdfDoc);
                        PdfAccessibilityTagger.writeInvisibleMCR(
                            canvas, tagPointer, overlayFont, px, py, child.text);
                        filledMcrCount++;
                        tagPointer.moveToRoot();
                    } catch (Exception e) {
                        try { tagPointer.moveToRoot(); }
                        catch (Exception ignored) {}
                    }
                }
            }
        }

        // Table captions: invisible MCR text into the Caption SEs created
        // in Step 1 (safe now — all invisible-text stripping is done).
        writeTableCaptionMCRs(pdfDoc, numPages, allElements,
            elementTreePath, overlayFont, tagPointer);

        if (filledMcrCount > 0 || emptyMcrCount > 0) {
            System.out.println("  Filled MCRs: " + filledMcrCount
                + " unmatched elements with invisible text, Empty MCRs: "
                + emptyMcrCount + " (no text)");
        }

        // --- Tag link annotations (PDF/UA 7.18.5) ---
        tagLinkAnnotations(pdfDoc, tagPointer, inlineLinkPaths);

        // --- Tag widget annotations (PDF/UA 7.18.4) ---
        tagWidgetAnnotations(pdfDoc, tagPointer);

        // Strip empty Form BDC/EMC markers from content streams.
        // tagWidgetAnnotations writes them for iText ParentTree registration,
        // then removes the MCR from the structure tree /K.  But the empty
        // BDC/EMC bytes remain in the content stream and cause Acrobat Read
        // Out Loud to report pages as empty.
        stripEmptyFormBDC(pdfDoc);

        // --- Tag markup annotations (PDF/UA 7.18.1) ---
        tagMarkupAnnotations(pdfDoc, tagPointer);

        // --- Compliance fixes ---
        PdfAccessibilityTagger.addMissingToUnicodeCMaps(pdfDoc, sourceFontRefs);
        PdfAccessibilityTagger.stripIncorrectCIDSets(pdfDoc);
        PdfAccessibilityTagger.stripSymbolicTrueTypeEncoding(pdfDoc);
        PdfAccessibilityTagger.fixCIDToGIDMap(pdfDoc);
        PdfAccessibilityTagger.embedMissingFonts(pdfDoc);
        PdfAccessibilityTagger.checkFontCompliance(pdfDoc, sourceFontRefs);
        PdfAccessibilityTagger.fixOCProperties(pdfDoc);
        PdfAccessibilityTagger.fixEmbeddedFileSpecs(pdfDoc);
        PdfAccessibilityTagger.setXmpMetadata(pdfDoc, title, lang);

        // iText builds ParentTree automatically on close()
        pdfDoc.close();
        System.out.println("Content-preserving tagging complete: "
            + tagged + " elements -> " + outputPath);
    }

    // ---------------------------------------------------------------
    // PdfCanvasEditor subclass that uses openTag/closeTag
    // ---------------------------------------------------------------

    /**
     * One-line warning for forced-Tr=3 write failures on a page, or
     * {@code null} when there were none.
     *
     * Kept separate from the editor so the message is unit-testable without
     * constructing a PDF — the same pattern as classifyDo/countUnbalancedBdc.
     */
    static String forceInvisibleWarning(int pageNum, int failures) {
        if (failures <= 0) {
            return null;
        }
        return "  WARNING: Page " + pageNum + ": failed to force Tr=3 on "
            + failures + " marked-content scope(s) — OCR text may render "
            + "VISIBLY over the scanned page image";
    }

    static class ContentPreservingEditor extends PdfCanvasEditor {
        final Map<Integer, ElementInfo> runMap;
        final Map<Integer, TagReference> imageToTagRef;
        final Map<Integer, TagReference> xobjectToTagRef;
        final TagTreePointer tagPointer;
        final Map<String, int[]> elementTreePath;
        final PdfPage page;
        /** True on "invisible-only" pages (whole-page Tr=3 OCR text, see
         *  invisibleOnlyPages above): re-asserts the source's own Tr=3 at
         *  the start of every new marked-content scope so each scope is
         *  self-contained.
         *
         *  NOTE on the mechanism: text rendering mode is graphics state and
         *  BDC/EMC does NOT scope graphics state, so a Tr=3 set once before
         *  the first word already persists into every later scope on its
         *  own.  An A/B of a scanned page that sets 3 Tr with and without this
         *  write produced pixel-identical renderings on all 18 genuine
         *  invisible-only pages (Tr=3 prevailing at 100% of text-showing
         *  operators either way), so this is a defensive, state-neutral
         *  re-assertion, not the cure for a page that paints visibly.
         *  It is only ever emitted when {@link #sourceTr} is already 3
         *  (see the guard in handleTextOperator), which is what keeps it
         *  from leaking Tr=3 onto content the source draws visibly. */
        final boolean forceInvisible;

        /** Text rendering mode currently prevailing in the SOURCE stream,
         *  tracked across Tr operators and q/Q save/restore.  The forced
         *  "3 Tr" write is gated on this being 3 so that the write can only
         *  ever restate the value the source already has in effect — it can
         *  never introduce a Tr=3 the source did not have, and so cannot
         *  suppress visible text drawn later on the page (including text
         *  inside a Form XObject, which ContentStreamAnalyzer does not
         *  recurse into and therefore cannot veto invisible-only
         *  classification). */
        int sourceTr = 0;
        private final Deque<Integer> sourceTrStack = new ArrayDeque<>();

        int taggedCount = 0;
        int artifactCount = 0;
        /** Failed "3 Tr" writes on an invisible-only page — see
         *  {@link ContentPreservingTagger#forceInvisibleWarning}. */
        int forceInvisibleFailures = 0;
        /** Failed replays of buffered text-state ops (Tm/Td/Tf/Tc/...) into
         *  the scope that needs them.  A drop here mispositions or
         *  misfonts everything downstream in the scope, so it is counted
         *  and reported rather than swallowed. */
        int textStateReplayFailures = 0;
        Set<String> taggedElementIds = new HashSet<>();
        int textRunIndex = 0;
        int imageIndex = 0;
        int xobjectDoIndex = 0;
        boolean inTextObject = false;
        boolean inMarkedContent = false;
        boolean inTaggedMC = false;  // true = openTag, false = beginMarkedContent
        boolean inArtifactWrap = false;
        Set<String> seenLBodyIds = new HashSet<>();

        /** The element ID currently open in a tagged BDC, or null.
         *  Used for per-element BDC grouping: the BDC stays open across
         *  consecutive words belonging to the same structure element.
         *  Acrobat's OCR text selection requires all words of a paragraph
         *  inside ONE BDC/EMC block — per-word BDC fragments the OCR layer
         *  and breaks selection for the first word of each element. */
        private String currentMcElemId = null;

        /** Buffered text-state/positioning operators (Tm, Td, Tf, Tc, etc.)
         *  captured at element boundaries.  When the next Tj belongs to a
         *  different element, the current BDC is closed and these ops are
         *  replayed inside the new BDC before the Tj.  This ensures Acrobat
         *  can find each element's first word's position/font within its
         *  marked content block. */
        java.io.ByteArrayOutputStream pendingTextStateOps =
            new java.io.ByteArrayOutputStream();

        /** Pre-scanned raw PdfString bytes for each text operator, indexed by
         *  text operator occurrence (aligned with textRunIndex).
         *  Each entry is a list of byte[] for PdfStrings in that operator
         *  (single entry for Tj, multiple for TJ array). */
        List<List<byte[]>> rawTextOpBytes;

        // Diagnostics for index alignment debugging
        final int totalRunMapEntries;
        final int maxRunIndex;

        ContentPreservingEditor(Map<Integer, ElementInfo> runMap,
                                 Map<Integer, TagReference> imageToTagRef,
                                 Map<Integer, TagReference> xobjectToTagRef,
                                 TagTreePointer tagPointer,
                                 Map<String, int[]> elementTreePath,
                                 PdfPage page,
                                 boolean forceInvisible) {
            super();
            this.runMap = runMap;
            this.imageToTagRef = imageToTagRef;
            this.xobjectToTagRef = xobjectToTagRef != null
                ? xobjectToTagRef : new HashMap<>();
            this.tagPointer = tagPointer;
            this.elementTreePath = elementTreePath;
            this.page = page;
            this.forceInvisible = forceInvisible;
            this.totalRunMapEntries = runMap.size();
            this.maxRunIndex = runMap.keySet().stream()
                .mapToInt(Integer::intValue).max().orElse(-1);
        }

        /**
         * Pre-scan raw content stream bytes to extract PdfString byte data
         * for each text operator (Tj, TJ, ', ").  iText's parser decodes
         * PdfString values through font encoding, losing original glyph IDs.
         * This method reads the raw bytes before any decoding.
         */
        void prescanRawTextBytes(byte[] contentBytes) {
            rawTextOpBytes = new ArrayList<>();
            com.itextpdf.io.source.PdfTokenizer tokenizer =
                new com.itextpdf.io.source.PdfTokenizer(
                    new com.itextpdf.io.source.RandomAccessFileOrArray(
                        new com.itextpdf.io.source.RandomAccessSourceFactory()
                            .createSource(contentBytes)));
            try {
                List<byte[]> pendingStrings = new ArrayList<>();
                boolean inArray = false;
                while (tokenizer.nextToken()) {
                    com.itextpdf.io.source.PdfTokenizer.TokenType type =
                        tokenizer.getTokenType();
                    if (type == com.itextpdf.io.source.PdfTokenizer.TokenType.String) {
                        byte[] rawContent = tokenizer.getByteContent();
                        boolean hex = tokenizer.isHexString();
                        byte[] raw = com.itextpdf.io.source.PdfTokenizer
                            .decodeStringContent(rawContent, hex);
                        pendingStrings.add(raw);
                    } else if (type == com.itextpdf.io.source.PdfTokenizer.TokenType.StartArray) {
                        inArray = true;
                    } else if (type == com.itextpdf.io.source.PdfTokenizer.TokenType.EndArray) {
                        inArray = false;
                    } else if (type == com.itextpdf.io.source.PdfTokenizer.TokenType.Other) {
                        String token = tokenizer.getStringValue();
                        if ("Tj".equals(token) || "TJ".equals(token)
                                || "'".equals(token) || "\"".equals(token)) {
                            rawTextOpBytes.add(new ArrayList<>(pendingStrings));
                            pendingStrings.clear();
                        } else if (!inArray) {
                            // Non-text operator outside array — discard
                            // pending strings (e.g., operands of Tf, Td).
                            // But keep strings if we're inside a TJ array
                            // since non-text tokens there are just numbers.
                            pendingStrings.clear();
                        }
                    } else if (type == com.itextpdf.io.source.PdfTokenizer.TokenType.Number) {
                        // Numbers (TJ array kerning or operator params) — skip
                    }
                }
                tokenizer.close();
            } catch (Exception e) {
                System.err.println("WARNING: prescanRawTextBytes failed: " + e);
                rawTextOpBytes = null;
            }
        }

        @Override
        protected void write(PdfCanvasProcessor processor,
                              PdfLiteral operator,
                              List<PdfObject> operands) {
            String op = operator.toString();

            // Track the SOURCE stream's prevailing text rendering mode.
            // Tr is graphics state: it survives BT/ET and BDC/EMC and is
            // only undone by a Q that restores a state saved before it.
            // The forced-invisible write below is gated on this value, so
            // it can only ever restate a Tr=3 the source already has.
            if ("Tr".equals(op)) {
                for (PdfObject o : operands) {
                    if (o instanceof PdfNumber) {
                        sourceTr = ((PdfNumber) o).intValue();
                    }
                }
            } else if ("q".equals(op)) {
                sourceTrStack.push(sourceTr);
            } else if ("Q".equals(op)) {
                if (!sourceTrStack.isEmpty()) {
                    sourceTr = sourceTrStack.pop();
                }
            }

            switch (op) {
                case "BT":
                    inTextObject = true;
                    closeArtifactWrap();
                    super.write(processor, operator, operands);
                    return;

                case "ET":
                    closePendingMC();
                    // Flush any trailing buffered text-state ops
                    if (pendingTextStateOps.size() > 0) {
                        try {
                            canvas.getContentStream().getOutputStream()
                                .writeBytes(pendingTextStateOps.toByteArray());
                        } catch (Exception ignored) {}
                        pendingTextStateOps.reset();
                    }
                    inTextObject = false;
                    super.write(processor, operator, operands);
                    return;

                case "Tj": case "TJ": case "'": case "\"":
                    handleTextOperator(processor, operator, operands);
                    return;

                case "BDC": case "BMC":
                    // Strip existing marked content
                    return;

                case "EMC":
                    // Strip existing EMC
                    return;

                case "Do":
                    // Classify via the analyzer's shared classifyDo — the
                    // analyzer and this writer maintain parallel form/image
                    // index counters, so both MUST classify every Do
                    // identically or all later tag references on the page
                    // shift by one (wrong alt text on wrong figures).
                    ContentStreamAnalyzer.XObjectKind doKind =
                        ContentStreamAnalyzer.classifyDo(page, operands);

                    if (doKind == ContentStreamAnalyzer.XObjectKind.OTHER) {
                        // Missing XObject, malformed operand, or non-image
                        // non-form subtype: the analyzer recorded nothing
                        // for it, so consume NO index. Keep it in the
                        // output verbatim, artifact-wrapped when not
                        // already inside marked content.
                        if (inArtifactWrap || inMarkedContent) {
                            super.write(processor, operator, operands);
                        } else {
                            canvas.beginMarkedContent(PdfName.Artifact);
                            super.write(processor, operator, operands);
                            canvas.endMarkedContent();
                            artifactCount++;
                        }
                        return;
                    }

                    if (doKind == ContentStreamAnalyzer.XObjectKind.FORM) {
                        // Form XObject — use xobjectToTagRef
                        TagReference xobjTagRef =
                            xobjectToTagRef.get(xobjectDoIndex);
                        if (xobjTagRef != null) {
                            closeArtifactWrap();
                            closePendingMC();
                            canvas.openTag(xobjTagRef);
                            super.write(processor, operator, operands);
                            canvas.closeTag();
                            taggedCount++;
                        } else if (inArtifactWrap || inMarkedContent) {
                            super.write(processor, operator, operands);
                        } else {
                            canvas.beginMarkedContent(PdfName.Artifact);
                            super.write(processor, operator, operands);
                            canvas.endMarkedContent();
                            artifactCount++;
                        }
                        xobjectDoIndex++;
                    } else {
                        // Image — use imageToTagRef (aligned with analyzer)
                        TagReference imgTagRef =
                            imageToTagRef.get(imageIndex);
                        if (imgTagRef != null) {
                            closeArtifactWrap();
                            closePendingMC();
                            canvas.openTag(imgTagRef);
                            super.write(processor, operator, operands);
                            canvas.closeTag();
                            taggedCount++;
                        } else if (inArtifactWrap || inMarkedContent) {
                            super.write(processor, operator, operands);
                        } else {
                            canvas.beginMarkedContent(PdfName.Artifact);
                            super.write(processor, operator, operands);
                            canvas.endMarkedContent();
                            artifactCount++;
                        }
                        imageIndex++;
                    }
                    return;

                default:
                    // Inside a text object with per-element BDC grouping:
                    // text-state ops (Tm, Td, Tf, Tc, etc.) between two Tj
                    // operators may straddle an element boundary.  If the
                    // next Tj belongs to the SAME element, write directly
                    // via super.write() to preserve iText's exact number
                    // formatting.  If different element, close the current
                    // BDC and buffer for replay in the next BDC.
                    if (inTextObject) {
                        String opStr = operator.toString();
                        if ("Tm".equals(opStr) || "Td".equals(opStr)
                                || "TD".equals(opStr) || "T*".equals(opStr)
                                || "Tf".equals(opStr) || "Tc".equals(opStr)
                                || "Tw".equals(opStr) || "Tz".equals(opStr)
                                || "TL".equals(opStr) || "Ts".equals(opStr)
                                || "Tr".equals(opStr)) {
                            // Look ahead: does the next text run belong to
                            // the same element as the currently open BDC?
                            ElementInfo nextElem = runMap.get(textRunIndex);
                            String nextId = (nextElem != null)
                                ? nextElem.id : null;
                            boolean sameElem = (nextId != null
                                && nextId.equals(currentMcElemId));

                            // Always buffer — handleTextOperator()
                            // replays inside the correct BDC.
                            StringBuilder sb = new StringBuilder();
                            for (PdfObject obj : operands) {
                                sb.append(obj.toString()).append(' ');
                            }
                            sb.append('\n');
                            try {
                                pendingTextStateOps.write(
                                    sb.toString().getBytes(
                                        java.nio.charset.StandardCharsets
                                            .ISO_8859_1));
                            } catch (java.io.IOException ignored) {}
                            return;
                        }
                    }
                    if (!inTextObject && !inMarkedContent) {
                        ensureArtifactWrap();
                    }
                    super.write(processor, operator, operands);
                    return;
            }
        }

        private void handleTextOperator(PdfCanvasProcessor processor,
                                         PdfLiteral operator,
                                         List<PdfObject> operands) {
            ElementInfo elem = runMap.get(textRunIndex);

            // Whitespace-only text runs (e.g. inter-word spaces) must be
            // artifact-wrapped, not tagged.  PAC flags tagged MCRs whose
            // painted glyphs are all whitespace as "tagged text consists
            // of only whitespace".
            if (elem != null && isWhitespaceOnlyTextOp(operands)) {
                elem = null;  // treat as unmatched → artifact
            }

            // Single-glyph leading run in a LBody is a bullet/label character.
            // Artifact-wrap it — the Lbl structure element carries /ActualText
            // for the label, and the visual bullet adds nothing for accessibility.
            if (elem != null && "LBody".equals(elem.tag)
                    && !seenLBodyIds.contains(elem.id)
                    && isSingleGlyphTextOp(operands)) {
                elem = null;
            }

            // Determine the element ID for this run
            String newElemId = (elem != null) ? elem.id : null;

            // Per-element BDC grouping: keep the BDC open if the next word
            // belongs to the SAME element.  Acrobat's OCR text selection
            // requires all words of a paragraph/heading inside ONE BDC/EMC
            // block — per-word BDC fragments the OCR layer and breaks
            // selection for the first word of each element.
            boolean sameElement = (newElemId != null
                    && newElemId.equals(currentMcElemId));

            if (!sameElement) {
                // Different element (or switching tagged↔artifact) — close
                // the previous BDC and open a new one.
                closePendingMC();

                if (elem != null) {
                    int[] path = elementTreePath.get(elem.id);
                    if (path != null) {
                        try {
                            tagPointer.moveToRoot();
                            for (int idx : path) {
                                tagPointer.moveToKid(idx);
                            }
                            tagPointer.setPageForTagging(page);
                            TagReference ref = tagPointer.getTagReference();
                            canvas.openTag(ref);
                            inMarkedContent = true;
                            inTaggedMC = true;
                            taggedCount++;
                            taggedElementIds.add(elem.id);
                            currentMcElemId = elem.id;
                            if ("LBody".equals(elem.tag)) {
                                seenLBodyIds.add(elem.id);
                            }
                            tagPointer.moveToRoot();
                        } catch (Exception e) {
                            canvas.beginMarkedContent(PdfName.Artifact);
                            inMarkedContent = true;
                            artifactCount++;
                            currentMcElemId = null;
                            try { tagPointer.moveToRoot(); }
                            catch (Exception ignored) {}
                        }
                    } else {
                        canvas.beginMarkedContent(PdfName.Artifact);
                        inMarkedContent = true;
                        artifactCount++;
                        currentMcElemId = null;
                    }
                } else {
                    // Unmatched run — artifact
                    canvas.beginMarkedContent(PdfName.Artifact);
                    inMarkedContent = true;
                    artifactCount++;
                    currentMcElemId = null;
                }
            }
            // else: sameElement — keep existing BDC open

            // Invisible-only pages: re-assert the source's own Tr=3 at the
            // start of every new BDC/Artifact scope so each scope carries
            // the rendering mode its text depends on instead of relying on
            // state set in an earlier, now-closed scope.
            //
            // This is defensive, not curative.  Tr is graphics state and
            // BDC/EMC does not scope it, so the source's single Tr=3 (set
            // once, before the first word) already persists into every
            // later scope; an A/B of a scanned page that sets 3 Tr with and
            // without this write rendered pixel-identically on all 18
            // genuine invisible-only pages.  What it buys is scope
            // self-containment: the invariant survives the buffered
            // text-state replay below failing, and survives any later
            // per-MCID extraction/reordering of the stream.
            //
            // The sourceTr == 3 guard is load-bearing: without it a page
            // misclassified as invisible-only — or one that draws visible
            // text through a Form XObject after the OCR layer, which
            // ContentStreamAnalyzer cannot see because it does not recurse
            // into forms — would have that visible text suppressed by a
            // Tr=3 that persists past EMC.  Measured: forcing unguarded on
            // Brandon page 19 (20 visible + 12 invisible text operators)
            // blanked the visible text, a 2.04% pixel delta against the
            // source; with the guard the page renders identically.
            if (!sameElement && forceInvisible && sourceTr == 3) {
                try {
                    canvas.getContentStream().getOutputStream()
                        .writeBytes("3 Tr\n".getBytes(StandardCharsets.ISO_8859_1));
                } catch (Exception e) {
                    forceInvisibleFailures++;
                }
            }

            // Replay buffered text-state/positioning ops (Tm, Tf, Tc, etc.)
            // inside this element's BDC, before the Tj.  Dropping these
            // silently would paint the rest of the scope at the wrong
            // position, size or font, so count failures and report them.
            if (pendingTextStateOps.size() > 0) {
                try {
                    canvas.getContentStream().getOutputStream()
                        .writeBytes(pendingTextStateOps.toByteArray());
                } catch (Exception e) {
                    textStateReplayFailures++;
                }
                pendingTextStateOps.reset();
            }

            writeTextOperatorPreserved(operator, operands);
            textRunIndex++;
        }

        /**
         * Check whether a text operator's operands paint only whitespace
         * or empty strings.  Handles Tj (single PdfString),
         * TJ (PdfArray of PdfString/PdfNumber), and ' / " (PdfString
         * after spacing numbers).
         *
         * Whitespace bytes: 0x20 (space in single-byte encodings) and
         * 0x00 (high byte of CID 0x0020 in Type0/Identity-H fonts, or
         * null padding).  A string like &lt;0020&gt; (CID space) is
         * two bytes: 0x00 then 0x20, both allowed.
         */
        private static boolean isWhitespaceOnlyTextOp(
                List<PdfObject> operands) {
            boolean hasAnyByte = false;
            for (PdfObject obj : operands) {
                if (obj instanceof PdfLiteral) continue; // operator itself
                if (obj instanceof PdfString) {
                    byte[] bytes = ((PdfString) obj).getValueBytes();
                    for (byte b : bytes) {
                        hasAnyByte = true;
                        int v = b & 0xFF;
                        if (v != 0x20 && v != 0x00) return false;
                    }
                } else if (obj instanceof PdfArray) {
                    PdfArray arr = (PdfArray) obj;
                    for (int i = 0; i < arr.size(); i++) {
                        PdfObject item = arr.get(i);
                        if (item instanceof PdfString) {
                            byte[] bytes = ((PdfString) item).getValueBytes();
                            for (byte b : bytes) {
                                hasAnyByte = true;
                                int v = b & 0xFF;
                                if (v != 0x20 && v != 0x00) return false;
                            }
                        }
                        // PdfNumber entries in TJ are spacing adjustments, ignore
                    }
                }
                // PdfNumber entries in ' / " are word/char spacing, ignore
            }
            return hasAnyByte;  // must have at least one byte to be "whitespace"
        }

        /**
         * Check whether a text operator paints exactly one glyph.
         * Single-byte encodings: 1 byte = 1 glyph.
         * Identity-H (Type0/CID): 2 bytes = 1 glyph.
         * Treats &le;2 total bytes as single-glyph to cover both cases.
         */
        private static boolean isSingleGlyphTextOp(
                List<PdfObject> operands) {
            int totalBytes = 0;
            for (PdfObject obj : operands) {
                if (obj instanceof PdfLiteral) continue;
                if (obj instanceof PdfString) {
                    totalBytes += ((PdfString) obj).getValueBytes().length;
                } else if (obj instanceof PdfArray) {
                    PdfArray arr = (PdfArray) obj;
                    for (int i = 0; i < arr.size(); i++) {
                        PdfObject item = arr.get(i);
                        if (item instanceof PdfString)
                            totalBytes += ((PdfString) item).getValueBytes().length;
                    }
                }
            }
            return totalBytes > 0 && totalBytes <= 2;
        }

        /**
         * Write a text operator (Tj, TJ, ', ") preserving raw PdfString bytes.
         * Uses hex string encoding (&lt;XX&gt;) instead of literal strings
         * to avoid iText's internal PdfString re-encoding which drops certain
         * byte values from subsetted font content (e.g. 'v','w','k' in Calibri).
         */
        private void writeTextOperatorPreserved(PdfLiteral operator,
                                                 List<PdfObject> operands) {
            rawStringIdx = 0;  // reset per-operator string index
            PdfOutputStream out =
                canvas.getContentStream().getOutputStream();
            for (PdfObject obj : operands) {
                if (obj instanceof PdfArray) {
                    out.writeByte((byte) '[');
                    PdfArray arr = (PdfArray) obj;
                    for (int i = 0; i < arr.size(); i++) {
                        PdfObject elem = arr.get(i);
                        if (elem instanceof PdfString) {
                            writeLiteralString(out, (PdfString) elem);
                        } else {
                            out.write(elem);
                        }
                        if (i < arr.size() - 1)
                            out.writeByte((byte) ' ');
                    }
                    out.writeByte((byte) ']');
                } else if (obj instanceof PdfString) {
                    writeLiteralString(out, (PdfString) obj);
                } else {
                    out.write(obj);
                }
                out.writeSpace();
            }
        }

        private static final char[] HEX =
            "0123456789ABCDEF".toCharArray();

        /** Index into rawTextOpBytes[textRunIndex-1] for current operator's strings. */
        private int rawStringIdx = 0;

        private void writeLiteralString(PdfOutputStream out, PdfString str) {
            byte[] bytes = null;
            // Use pre-scanned raw bytes if available
            int runIdx = textRunIndex; // current text op being written
            if (rawTextOpBytes != null && runIdx < rawTextOpBytes.size()) {
                List<byte[]> opBytes = rawTextOpBytes.get(runIdx);
                if (rawStringIdx < opBytes.size()) {
                    bytes = opBytes.get(rawStringIdx);
                    rawStringIdx++;
                }
            }
            if (bytes == null) {
                bytes = str.getValueBytes();  // fallback
            }

            // Check if literal encoding is safe (no C0 control bytes, balanced parens).
            // C0 controls (0x00-0x1F, incl. NUL, LF 0x0A, CR 0x0D) are unsafe in a
            // literal string: a raw CR/LF is EOL-normalized to a single LF on parse
            // (ISO 32000-1 §7.3.4.2), silently rewriting glyph code 0x0D->0x0A, and
            // iText mishandles 0x01-0x1F. Route any such string through the hex branch
            // below, which is immune. Java byte is signed, so 0x00-0x1F == (b>=0 && b<0x20);
            // high bytes (0x80-0xFF) are negative and correctly stay on the literal path.
            boolean useLiteral = true;
            int parenDepth = 0;
            for (byte b : bytes) {
                if (b >= 0 && b < 0x20) { useLiteral = false; break; }
                if (b == '(') parenDepth++;
                if (b == ')') { parenDepth--; if (parenDepth < 0) { useLiteral = false; break; } }
            }
            if (parenDepth != 0) useLiteral = false;

            if (useLiteral) {
                out.writeByte((byte) '(');
                for (byte b : bytes) {
                    if (b == '(' || b == ')' || b == '\\') {
                        out.writeByte((byte) '\\');
                    }
                    out.writeByte(b);
                }
                out.writeByte((byte) ')');
            } else {
                // Fallback to hex for bytes with NUL or unbalanced parens
                out.writeByte((byte) '<');
                for (byte b : bytes) {
                    out.writeByte((byte) HEX[(b >> 4) & 0x0F]);
                    out.writeByte((byte) HEX[b & 0x0F]);
                }
                out.writeByte((byte) '>');
            }
        }

        private void closePendingMC() {
            if (inMarkedContent) {
                if (inTaggedMC) {
                    canvas.closeTag();
                } else {
                    canvas.endMarkedContent();
                }
                inMarkedContent = false;
                inTaggedMC = false;
                currentMcElemId = null;
            }
        }

        private void ensureArtifactWrap() {
            if (!inArtifactWrap) {
                canvas.beginMarkedContent(PdfName.Artifact);
                inArtifactWrap = true;
                inMarkedContent = true;
                artifactCount++;
            }
        }

        private void closeArtifactWrap() {
            if (inArtifactWrap) {
                canvas.endMarkedContent();
                inArtifactWrap = false;
                inMarkedContent = false;
            }
        }

        @Override
        public void editContent(byte[] contentBytes,
                                 PdfResources resources,
                                 PdfCanvas canvas) {
            this.canvas = canvas;
            processContent(contentBytes, resources);
            // Flush at end of page
            if (inArtifactWrap) closeArtifactWrap();
            if (inMarkedContent) closePendingMC();
            this.canvas = null;
        }
    }

    // ---------------------------------------------------------------
    // Table hierarchy builder
    // ---------------------------------------------------------------

    /**
     * Create Table > [THead] > TR > TH/TD [> TBody > TR > TD] hierarchy.
     * Records tree paths for each cell so Step 2 can map content runs.
     * TagPointer must be positioned AT the Table element on entry.
     */
    private static void addTableHierarchy(
            TagTreePointer tagPointer,
            ElementInfo tableInfo,
            List<Integer> currentPath,
            int tableChildIdx,
            PdfDocument pdfDoc,
            int numPages,
            Map<String, int[]> elementTreePath,
            Map<Integer, Map<Integer, ElementInfo>> pageRunMap,
            Map<String, CellInfo> cellInfoById) {

        // Build base path to this Table element
        int[] tablePath = new int[currentPath.size() + 1];
        for (int j = 0; j < currentPath.size(); j++)
            tablePath[j] = currentPath.get(j);
        tablePath[currentPath.size()] = tableChildIdx;

        // Acrobat "Tables must have a summary": Summary is a standard TABLE
        // ATTRIBUTE (ISO 32000-1 §14.8.5.7) and belongs in the /A attribute
        // object — Acrobat's checker and its own "Edit Table Summary" UI
        // read/write /A, not a bare /Summary StructElem key. Use the real
        // caption when the structure JSON has one.
        PdfStructElem tableElem = tagPointer.getContext()
            .getPointerStructElem(tagPointer);
        PdfDictionary tableAttrs = new PdfDictionary();
        tableAttrs.put(new PdfName("O"), new PdfName("Table"));
        tableAttrs.put(new PdfName("Summary"), new PdfString(
            tableInfo.caption != null ? tableInfo.caption : "Data table",
            PdfEncodings.UNICODE_BIG));
        tableElem.put(PdfName.A, tableAttrs);

        // Caption must be the Table's FIRST child (ISO 32000-1 Table 337).
        // Create the (empty) Caption SE now and record its path; its
        // invisible MCR text is written by writeTableCaptionMCRs() AFTER
        // content-stream editing — writing it here would get deleted by
        // the stripInvisibleText pre-pass.
        // The Caption occupies kid index 0, so every kid index recorded
        // into elementTreePath below must start at 1 instead of 0.
        boolean hasCaption = tableInfo.caption != null;
        int kidOffset = 0;
        if (hasCaption) {
            tagPointer.addTag("Caption");
            int[] captionPath = new int[tablePath.length + 1];
            System.arraycopy(tablePath, 0, captionPath, 0, tablePath.length);
            captionPath[tablePath.length] = 0;
            elementTreePath.put(tableInfo.id + "::caption", captionPath);
            tagPointer.moveToParent(); // back to Table
            kidOffset = 1;
        }

        // Group cells by row (TreeMap preserves row order)
        TreeMap<Integer, List<CellInfo>> rows = new TreeMap<>();
        for (CellInfo ci : tableInfo.cells) {
            rows.computeIfAbsent(ci.row, k -> new ArrayList<>()).add(ci);
        }
        // Sort each row by column
        for (List<CellInfo> rowCells : rows.values()) {
            rowCells.sort(Comparator.comparingInt(c -> c.col));
        }

        // --- Span fixing pipeline (ported from PdfAccessibilityTagger) ---

        // 1. Build spanningCellStarts: origin positions of cells with RS>1
        Set<String> spanningCellStarts = new HashSet<>();
        for (CellInfo ci : tableInfo.cells) {
            if (ci.rowSpan > 1) {
                for (int dc = 0; dc < Math.max(ci.colSpan, 1); dc++) {
                    spanningCellStarts.add(ci.row + "," + (ci.col + dc));
                }
            }
        }

        // 2. Build spannedFromAbove: positions covered by rowspan from
        //    a prior row, unless a new spanning cell starts there.
        Set<String> spannedFromAbove = new HashSet<>();
        for (CellInfo ci : tableInfo.cells) {
            int rs = Math.max(ci.rowSpan, 1);
            int cs = Math.max(ci.colSpan, 1);
            for (int dr = 1; dr < rs; dr++) {
                for (int dc = 0; dc < cs; dc++) {
                    String key = (ci.row + dr) + "," + (ci.col + dc);
                    if (!spanningCellStarts.contains(key)) {
                        spannedFromAbove.add(key);
                    }
                }
            }
        }

        // 3. Remove duplicate cells at spannedFromAbove positions.
        //    Docling sometimes provides explicit cells at every position,
        //    but veraPDF counts both the spanning cell (via RowSpan)
        //    AND the explicit cell, resulting in too many columns.
        for (Map.Entry<Integer, List<CellInfo>> entry : rows.entrySet()) {
            int rowKey = entry.getKey();
            entry.getValue().removeIf(ci ->
                spannedFromAbove.contains(rowKey + "," + ci.col));
        }

        // 4. RowSpan clipping: when two spanning cells overlap in the
        //    same column, clip the earlier cell's RS to avoid double-counting.
        for (CellInfo ci : tableInfo.cells) {
            if (ci.rowSpan > 1) {
                for (CellInfo other : tableInfo.cells) {
                    if (other == ci) continue;
                    if (other.rowSpan > 1 && other.row > ci.row
                            && other.col == ci.col
                            && other.row < ci.row + ci.rowSpan) {
                        ci.rowSpan = other.row - ci.row;
                    }
                }
            }
        }

        // 5. ColSpan clipping: when cells in the same row overlap
        //    horizontally, clip the earlier cell's CS.
        for (List<CellInfo> rowCells : rows.values()) {
            rowCells.sort(Comparator.comparingInt(c -> c.col));
            for (int i = 0; i < rowCells.size() - 1; i++) {
                CellInfo curr = rowCells.get(i);
                CellInfo next = rowCells.get(i + 1);
                if (curr.col + curr.colSpan > next.col) {
                    int clipped = next.col - curr.col;
                    curr.colSpan = Math.max(clipped, 1);
                }
            }
        }

        // 6. Recompute grid dimensions after clipping
        int gridWidth = 0;
        int maxRow = 0;
        Set<String> covered = new HashSet<>();
        for (CellInfo ci : tableInfo.cells) {
            // Skip cells that were removed from rows (spannedFromAbove dups)
            boolean inRows = false;
            List<CellInfo> rowList = rows.get(ci.row);
            if (rowList != null && rowList.contains(ci)) inRows = true;
            if (!inRows) continue;
            for (int dr = 0; dr < Math.max(ci.rowSpan, 1); dr++) {
                for (int dc = 0; dc < Math.max(ci.colSpan, 1); dc++) {
                    covered.add((ci.row + dr) + "," + (ci.col + dc));
                    gridWidth = Math.max(gridWidth, ci.col + dc + 1);
                }
            }
            maxRow = Math.max(maxRow, ci.row);
        }

        // 7. Gap-fill: insert synthetic empty cells for uncovered positions
        //    that aren't spanned from above.
        for (int r = 0; r <= maxRow; r++) {
            for (int col = 0; col < gridWidth; col++) {
                String key = r + "," + col;
                if (spannedFromAbove.contains(key)) continue;
                if (covered.contains(key)) continue;
                CellInfo filler = new CellInfo(r, col, 1, 1, "",
                    false, false, false, new ArrayList<>(), tableInfo.pageNo);
                filler.cellId = tableInfo.id + "_fill_r" + r + "c" + col;
                rows.computeIfAbsent(r, k -> new ArrayList<>()).add(filler);
                covered.add(key);
            }
            // Re-sort after adding fillers
            List<CellInfo> rowList = rows.get(r);
            if (rowList != null) {
                rowList.sort(Comparator.comparingInt(c -> c.col));
            }
        }

        // First-row-TH fallback for tables with no header flags at all
        promoteFirstRowHeaders(rows);

        // Detect header rows: first contiguous rows where all cells are headers
        Set<Integer> headerRows = new LinkedHashSet<>();
        for (Map.Entry<Integer, List<CellInfo>> e : rows.entrySet()) {
            boolean allHeader = e.getValue().stream()
                .allMatch(c -> c.isHeader || c.isColHeader);
            if (allHeader) {
                headerRows.add(e.getKey());
            } else {
                break; // stop at first non-header row
            }
        }
        Set<Integer> bodyRows = new LinkedHashSet<>(rows.keySet());
        bodyRows.removeAll(headerRows);

        boolean useTHeadTBody = !headerRows.isEmpty() && !bodyRows.isEmpty();

        // Track child index within Table for path building.
        // Starts after the Caption (kid 0) when one was inserted.
        int tableKidIdx = kidOffset;

        PdfPage tablePage = pdfDoc.getPage(
            Math.min(tableInfo.pageNo, numPages));
        tagPointer.setPageForTagging(tablePage);

        if (useTHeadTBody) {
            // --- THead ---
            tagPointer.addTag("THead");
            int theadIdx = tableKidIdx++;
            addTableRows(tagPointer, rows, headerRows, tableInfo,
                tablePath, theadIdx, 0, gridWidth, tablePage,
                elementTreePath, pageRunMap, cellInfoById);
            tagPointer.moveToParent(); // back to Table

            // --- TBody ---
            tagPointer.addTag("TBody");
            int tbodyIdx = tableKidIdx++;
            addTableRows(tagPointer, rows, bodyRows, tableInfo,
                tablePath, tbodyIdx, 0, gridWidth, tablePage,
                elementTreePath, pageRunMap, cellInfoById);
            tagPointer.moveToParent(); // back to Table
        } else {
            // Flat: Table > TR > TH/TD (no THead/TBody wrapper)
            addTableRows(tagPointer, rows, rows.keySet(), tableInfo,
                tablePath, -1, kidOffset, gridWidth, tablePage,
                elementTreePath, pageRunMap, cellInfoById);
        }
    }

    /**
     * Write each table caption as invisible (Tr=3) MCR text inside the
     * Caption SE created by addTableHierarchy. MUST run after ALL
     * content-stream editing: the Step-2 stripInvisibleText pre-pass
     * deletes Tr=3 text written any earlier.
     */
    private static void writeTableCaptionMCRs(
            PdfDocument pdfDoc, int numPages,
            List<ElementInfo> allElements,
            Map<String, int[]> elementTreePath,
            PdfFont overlayFont,
            TagTreePointer tagPointer) {
        if (overlayFont == null) return;
        int written = 0;
        for (ElementInfo ei : allElements) {
            if (!"Table".equals(ei.tag) || ei.caption == null) continue;
            int[] capPath = elementTreePath.get(ei.id + "::caption");
            if (capPath == null) continue;
            try {
                PdfPage page = pdfDoc.getPage(Math.min(ei.pageNo, numPages));
                PdfCanvas canvas = getAppendCanvas(page, pdfDoc);
                int rot = 0;
                PdfNumber rotObj = page.getPdfObject()
                    .getAsNumber(PdfName.Rotate);
                if (rotObj != null) rot = rotObj.intValue();
                float mbW = page.getMediaBox().getWidth();
                float mbH = page.getMediaBox().getHeight();
                tagPointer.moveToRoot();
                for (int idx : capPath) tagPointer.moveToKid(idx);
                tagPointer.setPageForTagging(page);
                if (ei.hasBbox) {
                    // Thin strip along the table's top edge; fit-to-bbox
                    // layout sizes the invisible text to fit it.
                    PdfAccessibilityTagger.writeDistributedMCR(
                        canvas, tagPointer, overlayFont, ei.caption,
                        ei.bboxL, ei.bboxT, ei.bboxR, ei.bboxT + 14,
                        rot, mbW, mbH);
                } else {
                    float[] rcs = FormImagePageTagger.displayToContent(
                        72, 72, rot, mbW, mbH);
                    PdfAccessibilityTagger.writeInvisibleMCR(
                        canvas, tagPointer, overlayFont,
                        rcs[0], rcs[1], ei.caption);
                }
                written++;
                tagPointer.moveToRoot();
            } catch (Exception e) {
                try { tagPointer.moveToRoot(); } catch (Exception ignored) {}
                System.err.println("  Caption MCR failed for " + ei.id
                    + ": " + e.getMessage());
            }
        }
        if (written > 0) {
            System.err.println("  Wrote " + written
                + " table caption MCR(s)");
        }
    }

    /**
     * Add TR > TH/TD rows for a set of row indices.
     * If groupIdx >= 0, we're inside a THead/TBody (extra path level).
     */
    private static void addTableRows(
            TagTreePointer tagPointer,
            TreeMap<Integer, List<CellInfo>> allRows,
            Set<Integer> rowIndices,
            ElementInfo tableInfo,
            int[] tablePath,
            int groupIdx,
            int trStartIdx,
            int gridWidth,
            PdfPage page,
            Map<String, int[]> elementTreePath,
            Map<Integer, Map<Integer, ElementInfo>> pageRunMap,
            Map<String, CellInfo> cellInfoById) {

        // trStartIdx > 0 when a Caption SE occupies kid 0 of the Table and
        // the TRs are DIRECT Table children (flat case, groupIdx < 0) —
        // recorded paths are absolute kid indices and must skip it.
        int trIdx = trStartIdx;
        for (int rowNum : rowIndices) {
            List<CellInfo> cells = allRows.get(rowNum);
            if (cells == null || cells.isEmpty()) continue;

            tagPointer.addTag("TR");
            int cellIdx = 0;

            // Compute this row's effective column count (including
            // cells from previous rows that span into this row)
            int rowColSpan = 0;
            for (CellInfo ci : cells) {
                rowColSpan += ci.colSpan;
            }
            // Add columns covered by rowSpan from earlier rows
            for (Map.Entry<Integer, List<CellInfo>> otherEntry : allRows.entrySet()) {
                if (otherEntry.getKey() >= rowNum) break;
                for (CellInfo oci : otherEntry.getValue()) {
                    if (oci.rowSpan > 1 && oci.row + oci.rowSpan > rowNum) {
                        rowColSpan += oci.colSpan;
                    }
                }
            }

            for (CellInfo ci : cells) {
                String cellTag = (ci.isHeader || ci.isColHeader || ci.isRowHeader)
                    ? "TH" : "TD";
                tagPointer.addTag(cellTag);

                PdfStructElem cellElem = tagPointer.getContext()
                    .getPointerStructElem(tagPointer);

                // Set cell attributes: /O /Table, ColSpan, RowSpan, Scope
                PdfDictionary attrs = new PdfDictionary();
                attrs.put(new PdfName("O"), new PdfName("Table"));
                if (ci.colSpan > 1)
                    attrs.put(new PdfName("ColSpan"), new PdfNumber(ci.colSpan));
                if (ci.rowSpan > 1)
                    attrs.put(new PdfName("RowSpan"), new PdfNumber(ci.rowSpan));
                if ("TH".equals(cellTag)) {
                    String scope = ci.isColHeader ? "Column"
                        : (ci.isRowHeader ? "Row" : "Column");
                    attrs.put(new PdfName("Scope"), new PdfName(scope));
                }
                cellElem.put(PdfName.A, attrs);

                // /ActualText is intentionally NOT set on TD/TH leaves (F1 fix).
                // Phase 0 finding: /ActualText blocks AROL/NVDA from reading
                // MCR content. Cells with matched runs get BDC/EMC MCR
                // content; cells without runs become genuinely empty rather
                // than AROL-invisible placeholders.

                // Build tree path to this cell for Step 2 run mapping
                // Path: [...tablePath, groupIdx?, trIdx, cellIdx]
                int extraLevels = (groupIdx >= 0) ? 3 : 2; // group+TR+cell or TR+cell
                int[] cellPath = new int[tablePath.length + extraLevels];
                System.arraycopy(tablePath, 0, cellPath, 0, tablePath.length);
                int off = tablePath.length;
                if (groupIdx >= 0) {
                    cellPath[off++] = groupIdx;
                }
                cellPath[off++] = trIdx;
                cellPath[off] = cellIdx;

                // Always register cell path — overlay/form-image pages
                // need it even when there are no content runs.
                elementTreePath.put(ci.cellId, cellPath);
                cellInfoById.put(ci.cellId, ci);

                if (!ci.runRefs.isEmpty()) {
                    // Create a synthetic ElementInfo for this cell so the
                    // run→element map in Step 2 can find it
                    ElementInfo cellElement = new ElementInfo(
                        ci.cellId, cellTag, ci.text, null, tableInfo.pageNo);
                    for (int[] ref : ci.runRefs) {
                        pageRunMap.computeIfAbsent(ref[0], k -> new HashMap<>())
                            .put(ref[1], cellElement);
                    }
                }

                tagPointer.moveToParent(); // back to TR
                cellIdx++;
            }

            // Pad row with empty TD cells if shorter than grid width
            // (veraPDF 7.2 test 42: all rows must span same column count)
            while (rowColSpan < gridWidth) {
                tagPointer.addTag("TD");
                PdfStructElem padCell = tagPointer.getContext()
                    .getPointerStructElem(tagPointer);
                padCell.setActualText(new PdfString(" ",
                        PdfEncodings.UNICODE_BIG));
                tagPointer.moveToParent();
                rowColSpan++;
                cellIdx++;
            }

            tagPointer.moveToParent(); // back to Table or THead/TBody
            trIdx++;
        }
    }

    /**
     * Create L > LI > [Lbl +] LBody hierarchy.
     * Each LI gets its own content run mapping for per-run BDC.
     * TagPointer must be positioned AT the L element on entry.
     */
    private static void addListHierarchy(
            TagTreePointer tagPointer,
            ElementInfo listInfo,
            List<Integer> currentPath,
            int listChildIdx,
            PdfDocument pdfDoc,
            int numPages,
            Map<String, int[]> elementTreePath,
            Map<Integer, Map<Integer, ElementInfo>> pageRunMap) {

        int[] listPath = new int[currentPath.size() + 1];
        for (int j = 0; j < currentPath.size(); j++)
            listPath[j] = currentPath.get(j);
        listPath[currentPath.size()] = listChildIdx;

        PdfPage listPage = pdfDoc.getPage(
            Math.min(listInfo.pageNo, numPages));
        tagPointer.setPageForTagging(listPage);

        int liIdx = 0;
        for (ListItemInfo li : listInfo.listItems) {
            // LI
            tagPointer.addTag("LI");

            // Lbl (optional label — bullet/number).
            // Only emit Lbl in overlay/fallback mode (!liHasRuns) where the
            // label text is bound via /ActualText. When liHasRuns is true,
            // the bullet/number glyph lives in the LBody's content runs;
            // a separate Lbl SE would have no MCR to bind to and would
            // surface as an empty structure element.
            int lbodyChildIdx = 0;
            boolean liHasRuns = !li.runRefs.isEmpty();
            if (li.label != null && !li.label.isEmpty() && !liHasRuns) {
                tagPointer.addTag("Lbl");
                // /ActualText REMOVED — it blinds AROL/NVDA.  Record the Lbl's
                // tree path so the unmatched-element fill pass writes an
                // invisible (Tr=3) MCR carrying the label (SR Group B2).
                int[] lblPath = new int[listPath.length + 2];
                System.arraycopy(listPath, 0, lblPath, 0, listPath.length);
                lblPath[listPath.length] = liIdx;
                lblPath[listPath.length + 1] = 0; // Lbl is first child of LI
                elementTreePath.put(li.liId + "::lbl", lblPath);
                tagPointer.moveToParent(); // back to LI
                lbodyChildIdx = 1;
            }

            // LBody — /ActualText REMOVED (blinds AROL/NVDA).  When the LI has
            // no matched runs, the unmatched-element fill pass writes an
            // invisible (Tr=3) MCR for the LBody via the path recorded below
            // (SR Group B2).
            tagPointer.addTag("LBody");

            // Record tree path for LBody: listPath + [liIdx, lbodyChildIdx]
            // Always record path — overlay/form-image pages need it even
            // when there are no content runs to map.
            int[] lbodyPath = new int[listPath.length + 2];
            System.arraycopy(listPath, 0, lbodyPath, 0, listPath.length);
            lbodyPath[listPath.length] = liIdx;
            lbodyPath[listPath.length + 1] = lbodyChildIdx;
            elementTreePath.put(li.liId, lbodyPath);

            if (!li.runRefs.isEmpty()) {
                // Map LI content runs to this LBody element
                ElementInfo liElement = new ElementInfo(
                    li.liId, "LBody", li.text, null, listInfo.pageNo);
                for (int[] ref : li.runRefs) {
                    pageRunMap.computeIfAbsent(ref[0], k -> new HashMap<>())
                        .put(ref[1], liElement);
                }
            }

            tagPointer.moveToParent(); // back to LI
            tagPointer.moveToParent(); // back to L
            liIdx++;
        }
    }

    /**
     * Create TOCI → P hierarchy for TOC elements.
     * Each TOCI child gets a P child with /ActualText and content run mapping.
     * TagPointer must be positioned AT the TOC element on entry.
     */
    private static void addTocHierarchy(
            TagTreePointer tagPointer,
            ElementInfo tocInfo,
            List<Integer> currentPath,
            int tocChildIdx,
            PdfDocument pdfDoc,
            int numPages,
            Map<String, int[]> elementTreePath,
            Map<Integer, Map<Integer, ElementInfo>> pageRunMap,
            Map<Long, float[]> runXY,
            Map<Integer, List<LinkRect>> pageLinkRects) {

        int[] tocPath = new int[currentPath.size() + 1];
        for (int j = 0; j < currentPath.size(); j++)
            tocPath[j] = currentPath.get(j);
        tocPath[currentPath.size()] = tocChildIdx;

        PdfPage tocPage = pdfDoc.getPage(
            Math.min(tocInfo.pageNo, numPages));
        tagPointer.setPageForTagging(tocPage);

        int tociIdx = 0;
        for (TocItemInfo ti : tocInfo.tocItems) {
            // TOCI
            tagPointer.addTag("TOCI");

            // Does a PDF link annotation cover this TOC entry's runs?
            LinkRect tociLink = null;
            if (!ti.runRefs.isEmpty()) {
                for (int[] ref : ti.runRefs) {
                    LinkRect lr = linkForRun(ref[0], ref[1], runXY, pageLinkRects);
                    if (lr != null) { tociLink = lr; break; }
                }
            }
            boolean asLink = tociLink != null && !tociLink.consumed;

            // Leaf inside TOCI: Link when covered by an annotation, else P.
            // /ActualText REMOVED — it blocks AROL/NVDA from reading the
            // element (Phase-0 finding).  When the TOCI has no matched content
            // runs, the overlay fallback writes an invisible (Tr=3) MCR
            // carrying the entry text via the tree path recorded below; that
            // MCR is AROL-readable, so a /ActualText here is redundant and only
            // blinds AROL (screen_reader_sim Group B1).
            String leafTag = asLink ? "Link" : "P";
            tagPointer.addTag(leafTag);

            // Always record tree path for leaf: tocPath + [tociIdx, 0]
            // (0 = leaf is first child of TOCI)
            // Path must be stored unconditionally so the overlay fallback
            // can find and write MCRs for TOCI children without content runs.
            int[] pPath = new int[tocPath.length + 2];
            System.arraycopy(tocPath, 0, pPath, 0, tocPath.length);
            pPath[tocPath.length] = tociIdx;
            pPath[tocPath.length + 1] = 0;
            elementTreePath.put(ti.tociId, pPath);

            // Map TOCI content runs to this leaf element (only when runs exist)
            if (!ti.runRefs.isEmpty()) {
                ElementInfo tiElement = new ElementInfo(
                    ti.tociId, leafTag, ti.text, null, tocInfo.pageNo);
                for (int[] ref : ti.runRefs) {
                    pageRunMap.computeIfAbsent(ref[0], k -> new HashMap<>())
                        .put(ref[1], tiElement);
                }
            }

            // Attach the link annotation as an OBJR kid of the Link leaf.
            if (asLink) {
                attachLinkObjr(tagPointer, tociLink, pdfDoc, tocPage);
            }

            tagPointer.moveToParent(); // back to TOCI
            tagPointer.moveToParent(); // back to TOC
            tociIdx++;
        }
    }

    /**
     * Create P > Span + Link hierarchy for P elements with Docling Link children.
     * Content runs are mapped to the first Span child. Link children get
     * ActualText only (OBJR is added later by tagLinkAnnotations).
     * TagPointer must be positioned AT the P element on entry.
     */
    private static void addParagraphWithLinks(
            TagTreePointer tagPointer,
            ElementInfo pInfo,
            List<Integer> currentPath,
            int pChildIdx,
            PdfDocument pdfDoc,
            int numPages,
            Map<String, int[]> elementTreePath,
            Map<Integer, Map<Integer, ElementInfo>> pageRunMap,
            Map<String, InlineLinkInfo> inlineLinkPaths,
            boolean parentHasRuns,
            Map<Long, float[]> runXY,
            Map<Integer, List<LinkRect>> pageLinkRects) {

        // P path: currentPath + [pChildIdx]
        int[] pPath = new int[currentPath.size() + 1];
        for (int j = 0; j < currentPath.size(); j++)
            pPath[j] = currentPath.get(j);
        pPath[currentPath.size()] = pChildIdx;

        PdfPage page = pdfDoc.getPage(
            Math.min(pInfo.pageNo, numPages));
        tagPointer.setPageForTagging(page);

        // No ActualText on P — children carry it (avoids NestedAltText).

        // Plain / Span-only paragraphs reach here via geometric link coverage
        // (Step 5b) with no Docling children.  Normalize to an empty list so
        // the prefix-claim and child-creation loops are no-ops and the
        // geoLinkSegments branch drives child creation instead.
        List<ParagraphChildInfo> pChildren = pInfo.paragraphChildren != null
            ? pInfo.paragraphChildren : java.util.Collections.emptyList();

        boolean hasNonLinkChild = pChildren.stream()
            .anyMatch(c -> !"Link".equals(c.tag));

        // --- Distribute parent's runs across children via text-prefix match ---
        // Build per-child claimed-run sets BEFORE creating SEs so we know
        // which children should receive remap.  Known limitation: when a
        // single content-stream run spans two Docling children (one Tj op
        // emits text that Docling later split into separate Spans), the
        // run is claimed by the earlier child and the later child becomes
        // blank.  In that case the leftover-sweep below routes any final
        // unclaimed runs to the first non-Link Span — matching pre-fix
        // behaviour.
        int n = pChildren.size();
        @SuppressWarnings({"unchecked", "rawtypes"})
        java.util.Set<Long>[] claimedPerChild = new java.util.Set[n];
        for (int i = 0; i < n; i++) claimedPerChild[i] = new java.util.HashSet<>();
        int firstNonLinkIdx = -1;
        for (int i = 0; i < n; i++) {
            if (!"Link".equals(pChildren.get(i).tag)) {
                firstNonLinkIdx = i;
                break;
            }
        }

        List<RunRef> runs = pInfo.parentRunRefs;
        if (parentHasRuns && runs != null && !runs.isEmpty()) {
            int cursor = 0;
            for (int ci = 0; ci < n; ci++) {
                ParagraphChildInfo child = pChildren.get(ci);
                String target = normalizeForRunMatch(child.text);
                if (target.isEmpty()) continue;
                StringBuilder acc = new StringBuilder();
                int startCursor = cursor;
                while (cursor < runs.size()) {
                    RunRef rr = runs.get(cursor);
                    String rt = normalizeForRunMatch(rr.text);
                    if (rt.isEmpty()) {
                        // Whitespace-only / empty run.
                        if (acc.length() > 0) {
                            // Mid-child: include it (inter-word space).
                            claimedPerChild[ci].add(packRunKey(rr.page, rr.runIdx));
                            cursor++;
                            continue;
                        } else {
                            // Leading empty run before this child has started
                            // accumulating.  Claim it for this child and keep
                            // scanning — do NOT break, which would stall all
                            // subsequent children at the same run.
                            claimedPerChild[ci].add(packRunKey(rr.page, rr.runIdx));
                            cursor++;
                            continue;
                        }
                    }
                    // Try extending the accumulator with this run.
                    // Two candidates: space-joined and direct concatenation.
                    // Direct concat handles URLs/emails that content streams
                    // split at arbitrary code points (e.g. "www.myw"+"mu.com").
                    String tryAccSpace = acc.length() == 0 ? rt : (acc + " " + rt);
                    String tryAccConcat = acc.length() == 0 ? rt : (acc + rt);
                    boolean matchSpace = target.startsWith(tryAccSpace)
                            || tryAccSpace.equals(target)
                            || tryAccSpace.startsWith(target);
                    boolean matchConcat = !tryAccConcat.equals(tryAccSpace)
                            && (target.startsWith(tryAccConcat)
                            || tryAccConcat.equals(target)
                            || tryAccConcat.startsWith(target));
                    String tryAcc = matchConcat ? tryAccConcat : tryAccSpace;
                    if (matchSpace || matchConcat) {
                        // tryAcc fits inside target, or has just consumed it.
                        claimedPerChild[ci].add(packRunKey(rr.page, rr.runIdx));
                        acc.setLength(0); acc.append(tryAcc);
                        cursor++;
                        if (acc.length() >= target.length()) break;
                    } else {
                        // Run doesn't continue the prefix.  If we've claimed
                        // nothing for this child, advance one run so we don't
                        // deadlock on a docling-vs-pdf divergence.
                        if (cursor == startCursor) cursor++;
                        break;
                    }
                }
            }
            // Any remaining unclaimed runs: assign to the first non-Link child
            // (preserves the pre-fix behaviour for divergent / missed text).
            if (firstNonLinkIdx >= 0 && cursor < runs.size()) {
                for (int k = cursor; k < runs.size(); k++) {
                    RunRef rr = runs.get(k);
                    long key = packRunKey(rr.page, rr.runIdx);
                    boolean alreadyClaimed = false;
                    for (int j = 0; j < n; j++) {
                        if (claimedPerChild[j].contains(key)) { alreadyClaimed = true; break; }
                    }
                    if (!alreadyClaimed) claimedPerChild[firstNonLinkIdx].add(key);
                }
            }
        }

        // --- Geometric link refinement -------------------------------------
        // For each run of this paragraph, check whether it falls inside a PDF
        // link annotation rect. Group consecutive same-annotation runs (null =
        // ordinary text) into segments. Skip entirely when Docling already
        // produced a Link child (URL anchor) — that path owns its runs and gets
        // its OBJR via the existing matcher.
        boolean doclingHasLink = pChildren.stream()
            .anyMatch(c -> "Link".equals(c.tag));
        if (parentHasRuns && !doclingHasLink && pInfo.parentRunRefs != null) {
            List<Object[]> segments = new ArrayList<>(); // {LinkRect|null, String, List<Long>}
            for (RunRef rr : pInfo.parentRunRefs) {
                LinkRect lr = linkForRun(rr.page, rr.runIdx, runXY, pageLinkRects);
                String rtext = rr.text == null ? "" : rr.text;
                long key = packRunKey(rr.page, rr.runIdx);
                if (!segments.isEmpty() && segments.get(segments.size() - 1)[0] == lr) {
                    Object[] seg = segments.get(segments.size() - 1);
                    seg[1] = ((String) seg[1]) + rtext;
                    @SuppressWarnings("unchecked")
                    List<Long> ks = (List<Long>) seg[2];
                    ks.add(key);
                } else {
                    List<Long> ks = new ArrayList<>();
                    ks.add(key);
                    segments.add(new Object[]{lr, rtext, ks});
                }
            }
            boolean anyLink = segments.stream().anyMatch(s -> s[0] != null);
            if (anyLink) {
                pInfo.geoLinkSegments = segments;
            }
        }

        boolean firstRunTargetMapped = false;
        int childIdx = 0;
        if (pInfo.geoLinkSegments != null) {
            int segIdx = 0;
            for (Object[] seg : pInfo.geoLinkSegments) {
                LinkRect lr = (LinkRect) seg[0];
                String segText = (String) seg[1];
                @SuppressWarnings("unchecked")
                List<Long> runKeys = (List<Long>) seg[2];
                String segTag = lr != null ? "Link" : "Span";
                tagPointer.addTag(segTag);

                int[] segPath = new int[pPath.length + 1];
                System.arraycopy(pPath, 0, segPath, 0, pPath.length);
                segPath[pPath.length] = segIdx;

                String segId = pInfo.id + "_geo" + segIdx;
                elementTreePath.put(segId, segPath);
                ElementInfo segElem = new ElementInfo(segId, segTag, segText,
                        null, pInfo.pageNo);
                Set<Long> keySet = new HashSet<>(runKeys);
                remapSpecificRuns(pageRunMap, pInfo, segElem, keySet);

                if (lr != null && !lr.consumed) {
                    attachLinkObjr(tagPointer, lr, pdfDoc, page);
                }
                tagPointer.moveToParent();
                segIdx++;
            }
            return;
        }
        // Defensive: a plain / Span-only P routed here (empty paragraphChildren)
        // with NO geoLinkSegments would otherwise create no children and drop
        // its runs to artifact.  This should not happen — the new dispatch only
        // routes covered P's, and a covered P always yields ≥1 segment — but if
        // it does, register the P's own tree path so its runs (still mapped to
        // pInfo in pageRunMap) get tagged directly under this P by STEP 2,
        // rather than being orphaned/artifacted.
        if (pChildren.isEmpty()) {
            elementTreePath.put(pInfo.id, pPath);
            return;
        }
        for (ParagraphChildInfo child : pChildren) {
            String childTag = "Link".equals(child.tag) ? "Link" : "Span";
            tagPointer.addTag(childTag);

            // /ActualText on leaf children is intentionally NOT set — it blinds
            // AROL/NVDA from reading MCR content (F1 fix).  When the parent P
            // has NO matched runs, the child's path is recorded below so the
            // unmatched-element fill pass writes an invisible (Tr=3) MCR
            // carrying the child text instead (SR Group B2 — bibliography
            // Span/Link).
            boolean claimedAny = !claimedPerChild[childIdx].isEmpty();

            int[] childPath = new int[pPath.length + 1];
            System.arraycopy(pPath, 0, childPath, 0, pPath.length);
            childPath[pPath.length] = childIdx;

            if (!parentHasRuns) {
                elementTreePath.put(pInfo.id + "::pc" + childIdx, childPath);
            }

            if ("Link".equals(childTag)) {
                String key = child.text != null
                    ? child.text.replaceAll("[;,.)\\s]+$", "").trim() : "";
                if (!key.isEmpty()) {
                    inlineLinkPaths.put(key, new InlineLinkInfo(
                        childPath, pInfo.pageNo,
                        pInfo.bboxL, pInfo.bboxB, pInfo.bboxR, pInfo.bboxT,
                        pInfo.hasBbox, child.text));
                }

                // Link-only P: first Link receives content runs so they
                // are tagged instead of artifact-wrapped.
                if (!hasNonLinkChild && !firstRunTargetMapped) {
                    String linkId = pInfo.id + "_link0";
                    elementTreePath.put(linkId, childPath);
                    ElementInfo to = new ElementInfo(linkId, "Link", pInfo.text,
                            null, pInfo.pageNo);
                    if (claimedAny) {
                        remapSpecificRuns(pageRunMap, pInfo, to, claimedPerChild[childIdx]);
                    } else {
                        remapRunsFrom(pageRunMap, pInfo, to);
                    }
                    firstRunTargetMapped = true;
                } else if (claimedAny) {
                    // Inline Link child in a mixed P: remap just its claimed runs.
                    String linkId = pInfo.id + "_link" + childIdx;
                    elementTreePath.put(linkId, childPath);
                    ElementInfo to = new ElementInfo(linkId, "Link", child.text,
                            null, pInfo.pageNo);
                    remapSpecificRuns(pageRunMap, pInfo, to, claimedPerChild[childIdx]);
                }
            } else {
                // Non-Link child: remap its claimed runs.
                if (claimedAny) {
                    String spanId = pInfo.id + "_span" + childIdx;
                    elementTreePath.put(spanId, childPath);
                    ElementInfo to = new ElementInfo(spanId, "Span", child.text,
                            null, pInfo.pageNo);
                    remapSpecificRuns(pageRunMap, pInfo, to, claimedPerChild[childIdx]);
                    firstRunTargetMapped = true;
                } else if (!firstRunTargetMapped) {
                    // No claim AND no prior remap → fall back to the original
                    // "first non-Link Span eats everything" behaviour so we
                    // don't regress documents where prefix matching fails.
                    String spanId = pInfo.id + "_span0";
                    elementTreePath.put(spanId, childPath);
                    remapRunsFrom(pageRunMap, pInfo,
                        new ElementInfo(spanId, "Span", pInfo.text,
                            null, pInfo.pageNo));
                    firstRunTargetMapped = true;
                }
            }

            tagPointer.moveToParent(); // back to P
            childIdx++;
        }
    }

    /** Remap all pageRunMap entries pointing to {@code from} to {@code to}. */
    private static void remapRunsFrom(
            Map<Integer, Map<Integer, ElementInfo>> pageRunMap,
            ElementInfo from, ElementInfo to) {
        for (Map<Integer, ElementInfo> rm : pageRunMap.values()) {
            for (Map.Entry<Integer, ElementInfo> re : rm.entrySet()) {
                if (re.getValue() == from) {
                    re.setValue(to);
                }
            }
        }
    }

    /**
     * Normalize text for greedy run-prefix matching:
     *   - NFKC compose smart quotes / ligatures to ASCII equivalents
     *   - Lowercase (ROOT locale)
     *   - Replace em-dash, en-dash, bullet with space
     *   - Collapse all whitespace runs (including control chars) to a single space
     *
     * Matches output_gen/content_matcher.py:_normalize semantics closely
     * enough for prefix comparison.  This is intentionally lossy — exact
     * round-trip is not required, only stable prefix containment.
     */
    private static String normalizeForRunMatch(String s) {
        if (s == null || s.isEmpty()) return "";
        String t = java.text.Normalizer.normalize(s, java.text.Normalizer.Form.NFKC);
        t = t.replace('’', '\'').replace('‘', '\'');
        t = t.replace('”', '"').replace('“', '"');
        t = t.replace('—', ' ').replace('–', ' ');
        t = t.replace('•', ' ').replace(' ', ' ');
        t = t.toLowerCase(java.util.Locale.ROOT);
        // Strip ASCII control / whitespace runs to single space
        StringBuilder sb = new StringBuilder(t.length());
        boolean prevSpace = false;
        for (int i = 0; i < t.length(); i++) {
            char c = t.charAt(i);
            if (Character.isWhitespace(c) || c < 0x20) {
                if (!prevSpace && sb.length() > 0) sb.append(' ');
                prevSpace = true;
            } else {
                sb.append(c);
                prevSpace = false;
            }
        }
        int end = sb.length();
        while (end > 0 && sb.charAt(end - 1) == ' ') end--;
        return sb.substring(0, end);
    }

    /**
     * Remap a specified subset of pageRunMap entries from {@code from} to
     * {@code to}.  Each {@code claimedKeys} element is a packed long where
     * the high 32 bits are the page number and the low 32 bits are the
     * runIdx — chosen so the set can live on the stack without boxing.
     */
    private static void remapSpecificRuns(
            Map<Integer, Map<Integer, ElementInfo>> pageRunMap,
            ElementInfo from,
            ElementInfo to,
            java.util.Set<Long> claimedKeys) {
        if (pageRunMap == null || claimedKeys == null || claimedKeys.isEmpty()) return;
        for (long key : claimedKeys) {
            int page = (int) (key >>> 32);
            int runIdx = (int) (key & 0xFFFFFFFFL);
            Map<Integer, ElementInfo> rm = pageRunMap.get(page);
            if (rm == null) continue;
            ElementInfo cur = rm.get(runIdx);
            if (cur == from) {
                rm.put(runIdx, to);
            }
        }
    }

    /** Pack (page, runIdx) into a single long: high 32 bits = page, low 32 bits = runIdx. */
    private static long packRunKey(int page, int runIdx) {
        return (((long) page) << 32) | (runIdx & 0xFFFFFFFFL);
    }

    /**
     * Recursively collect run positions ({page, run_index, x, y}) from every
     * content_run_indices array anywhere in the structure JSON — including
     * nested TOCI children, table cells, and list items — into runXY. The
     * top-level element loop skips TOC/Table/L for run→element mapping, so
     * those run positions would otherwise never be recorded for geometric
     * link matching.
     */
    private static void harvestRunPositions(JsonArray elements,
                                            Map<Long, float[]> runXY) {
        if (elements == null) return;
        for (JsonElement je : elements) {
            if (!je.isJsonObject()) continue;
            JsonObject obj = je.getAsJsonObject();
            harvestRunPositionsFromNode(obj, runXY);
        }
    }

    private static void harvestRunPositionsFromNode(JsonObject obj,
                                                    Map<Long, float[]> runXY) {
        // Harvest this node's own content_run_indices
        if (obj.has("content_run_indices")
                && obj.get("content_run_indices").isJsonArray()) {
            for (JsonElement re : obj.getAsJsonArray("content_run_indices")) {
                if (!re.isJsonObject()) continue;
                JsonObject rr = re.getAsJsonObject();
                if (rr.has("page") && rr.has("run_index")
                        && rr.has("x") && rr.has("y")) {
                    int page = rr.get("page").getAsInt();
                    int runIdx = rr.get("run_index").getAsInt();
                    float x = rr.get("x").getAsFloat();
                    float y = rr.get("y").getAsFloat();
                    runXY.putIfAbsent(packRunKey(page, runIdx),
                        new float[]{x, y});
                }
            }
        }
        // Recurse into common child-array properties that can carry runs.
        for (String prop : new String[]{"children", "cells", "list_items",
                                         "items", "rows"}) {
            if (obj.has(prop) && obj.get(prop).isJsonArray()) {
                for (JsonElement ce : obj.getAsJsonArray(prop)) {
                    if (ce.isJsonObject()) {
                        harvestRunPositionsFromNode(ce.getAsJsonObject(), runXY);
                    }
                }
            }
        }
    }

    /**
     * Alt text for a Figure, or null when there is none to write.
     *
     * Deliberately returns null rather than a placeholder: veraPDF, PAC and
     * Ally all accept any non-empty /Alt, so writing "Figure" made an
     * information-free document score as compliant while a screen reader
     * read "Figure, Figure, Figure". Omitting /Alt fails PDF/UA 7.3
     * honestly, which is what an alt-less figure actually is.
     */
    static String resolveFigureAlt(ElementInfo info) {
        if (info.altText != null && !info.altText.trim().isEmpty()) {
            return info.altText;
        }
        return null;
    }

    /**
     * Alt text for a Formula, or null when there is none to write.
     *
     * Unlike the bare "Formula" label this replaces, "Formula: " + text is
     * derived from the decoded formula content and does carry information,
     * so it stays as a fallback.
     */
    static String resolveFormulaAlt(ElementInfo info) {
        if (info.altText != null && !info.altText.trim().isEmpty()) {
            return info.altText;
        }
        if (isUsableText(info.text)) {
            return "Formula: " + info.text;
        }
        return null;
    }

    /** A form fill-in placeholder: an underscore inside square brackets. */
    private static final java.util.regex.Pattern FORM_PLACEHOLDER =
        java.util.regex.Pattern.compile("\\[[^\\]]*_[^\\]]*\\]");

    static boolean isUsableText(String text) {
        if (text == null || text.trim().isEmpty()) return false;
        String t = text.trim();
        if (t.startsWith("/")) return false;       // CID garbled (Type3 fonts)
        if (t.startsWith("[") || t.startsWith("{")) return false; // form placeholders
        if (FORM_PLACEHOLDER.matcher(t).find()) return false;  // e.g. "[Name_here]"
        return true;
    }

    /**
     * True when an element's readable text lives in parsed children
     * (list items, table cells, TOC items) rather than its own text.
     * Docling ListGroups have empty top-level text; without this the
     * needsTag gate drops them entirely on overlay pages.
     */
    static boolean hasChildContent(ElementInfo info) {
        if (info.listItems != null) {
            for (ListItemInfo li : info.listItems) {
                if (isUsableText(li.text) || isUsableText(li.label)) return true;
            }
        }
        if (info.cells != null) {
            for (CellInfo ci : info.cells) {
                if (isUsableText(ci.text)) return true;
            }
        }
        if (info.tocItems != null) {
            for (TocItemInfo ti : info.tocItems) {
                if (isUsableText(ti.text)) return true;
            }
        }
        return false;
    }

    // ---------------------------------------------------------------
    // Helpers
    // ---------------------------------------------------------------

    /**
     * Strip empty /Form BDC/EMC markers from all page content streams.
     * tagWidgetAnnotations() writes these for iText ParentTree registration,
     * then removes the MCR from the structure tree.  The empty BDC/EMC bytes
     * remain in the content stream and cause Acrobat Read Out Loud to report
     * pages as empty.
     */
    private static void stripEmptyFormBDC(PdfDocument pdfDoc) {
        java.util.regex.Pattern pattern = java.util.regex.Pattern.compile(
            "/Form\\s*<<[^>]*>>\\s*BDC\\s*\\n?EMC\\s*\\n?");
        int totalStripped = 0;

        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfPage page = pdfDoc.getPage(p);
            try {
                byte[] content = page.getContentBytes();
                if (content == null || content.length == 0) continue;

                String cs = new String(content,
                    java.nio.charset.StandardCharsets.ISO_8859_1);
                java.util.regex.Matcher m = pattern.matcher(cs);
                if (!m.find()) continue;

                String cleaned = m.replaceAll("");
                int stripped = 0;
                m.reset();
                while (m.find()) stripped++;
                stripped++; // first find above

                page.put(PdfName.Contents, new PdfStream(
                    cleaned.getBytes(
                        java.nio.charset.StandardCharsets.ISO_8859_1)));
                totalStripped += stripped;
            } catch (Exception e) {
                System.err.println("  Warning: stripEmptyFormBDC page "
                    + p + ": " + e.getMessage());
            }
        }
        if (totalStripped > 0) {
            System.out.println("  Stripped " + totalStripped
                + " empty Form BDC/EMC markers from content streams");
        }
    }

    /**
     * First-row-TH fallback: iff NO cell anywhere in the table carries a
     * header flag and the table has >=2 rows, promote every first-row
     * cell to a column header so downstream header-row detection emits
     * THead/TH with /Scope /Column (Ally "TableHeaders"; same promotion
     * Acrobat's own fix applies). Gated by --first-row-th-fallback.
     *
     * @return true if promotion was applied
     */
    static boolean promoteFirstRowHeaders(
            TreeMap<Integer, List<CellInfo>> rows) {
        if (!PdfAccessibilityTagger.firstRowThFallback) return false;
        if (rows.size() < 2) return false;
        for (List<CellInfo> rowCells : rows.values()) {
            for (CellInfo c : rowCells) {
                if (c.isHeader || c.isColHeader || c.isRowHeader) {
                    return false;
                }
            }
        }
        List<CellInfo> firstRow = rows.firstEntry().getValue();
        // Gap-fill can synthesize an all-empty row 0 when the source table's
        // real cells start at row index >=1 (e.g. a caption/spacer row). Don't
        // promote empty filler cells to TH.
        boolean firstRowHasText = false;
        for (CellInfo c : firstRow) {
            if (c.text != null && !c.text.trim().isEmpty()) {
                firstRowHasText = true;
                break;
            }
        }
        if (!firstRowHasText) return false;
        for (CellInfo c : firstRow) {
            c.isColHeader = true;
        }
        return true;
    }

    /**
     * Replace a page's /Contents with a fresh, non-shared single stream.
     * <p>
     * Some PDF producers reuse a single content-stream object as the first
     * /Contents entry of every page (a shared " q " save-state prologue).
     * Writing a page's full content into that object via
     * {@code getContentStream(0).setData(...)} corrupts the shared stream and
     * cross-contaminates every other page that references it — which both
     * duplicates content and triggers a NullPointerException in iText when a
     * later page's /Resources doesn't declare a colorspace the poisoned content
     * now uses. Replacing /Contents with a brand-new stream leaves the shared
     * object untouched.
     * <p>
     * Mirrors the in-codebase idiom at the metadata-only path and
     * wrapPageAsArtifact(); no setModified() is needed because put() marks the
     * page dictionary modified.
     */
    static void replacePageContents(PdfDocument doc, PdfPage page,
                                    byte[] cleaned) {
        page.put(PdfName.Contents, new PdfStream(cleaned).makeIndirect(doc));
    }

    private static void wrapPageAsArtifact(PdfDocument pdfDoc, PdfPage page)
            throws Exception {
        byte[] content = page.getContentBytes();
        if (content == null || content.length == 0) return;

        PdfCanvas canvas = new PdfCanvas(
            new PdfStream(), page.getResources(), pdfDoc);
        canvas.beginMarkedContent(PdfName.Artifact);
        canvas.saveState();  // isolate CTM from original content
        canvas.getContentStream().getOutputStream().writeBytes(content);
        canvas.getContentStream().getOutputStream().writeNewLine();
        canvas.restoreState();
        canvas.endMarkedContent();

        page.put(PdfName.Contents, canvas.getContentStream());
    }

    private static void clearExistingStructure(PdfDocument pdfDoc) {
        PdfDictionary stRoot = pdfDoc.getCatalog().getPdfObject()
            .getAsDictionary(PdfName.StructTreeRoot);
        if (stRoot != null) {
            stRoot.remove(PdfName.K);
            stRoot.remove(new PdfName("ParentTree"));
            stRoot.remove(new PdfName("ParentTreeNextKey"));
            PdfAccessibilityTagger.stripInheritedRoleMap(stRoot);
        }
        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfPage pg = pdfDoc.getPage(p);
            pg.getPdfObject().remove(new PdfName("StructParents"));
            var annots = pg.getAnnotations();
            if (annots != null) {
                for (var ann : annots) {
                    ann.getPdfObject().remove(PdfName.StructParent);
                }
            }
        }
        // Clean ghost StructElems — must catch both explicit (/Type /StructElem)
        // and implicit (just /S without /Type) structure elements from source PDF
        for (int xref = 1; xref < pdfDoc.getNumberOfPdfObjects(); xref++) {
            try {
                PdfObject obj = pdfDoc.getPdfObject(xref);
                if (obj == null || !obj.isDictionary()) continue;
                PdfDictionary dict = (PdfDictionary) obj;
                PdfName type = dict.getAsName(PdfName.Type);
                boolean isExplicitStructElem = PdfName.StructElem.equals(type);
                // Implicit StructElem: has /S (role) and /P (parent) but no /Type
                boolean isImplicitStructElem = !isExplicitStructElem
                    && dict.containsKey(PdfName.S)
                    && dict.containsKey(PdfName.P);
                if (isExplicitStructElem || isImplicitStructElem) {
                    dict.remove(PdfName.Type);
                    dict.remove(PdfName.S);
                    dict.remove(PdfName.Pg);
                    dict.remove(PdfName.K);
                    dict.remove(PdfName.P);
                    dict.remove(new PdfName("Alt"));
                    dict.remove(new PdfName("ActualText"));
                    dict.remove(PdfName.A);
                }
            } catch (Exception e) {
                System.err.println("WARNING: Skipping malformed object " + xref
                    + " in clearExistingStructure: " + e.getMessage());
            }
        }
    }

    // ---------------------------------------------------------------
    // Link Annotation Tagging
    // ---------------------------------------------------------------

    /**
     * Tag link annotations in the structure tree (PDF/UA 7.18.5).
     * When an inline Link SE exists (from P elements with Docling Link
     * children), navigates to that SE and adds OBJR there. Otherwise
     * creates P > Link structure at the Document root as fallback.
     */
    /** Collect link annotations per page with their /Contents description. */
    private static Map<Integer, List<LinkRect>> buildLinkRectIndex(PdfDocument pdfDoc) {
        Map<Integer, List<LinkRect>> idx = new HashMap<>();
        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfPage page = pdfDoc.getPage(p);
            List<PdfAnnotation> annots = page.getAnnotations();
            if (annots == null || annots.isEmpty()) continue;
            for (PdfAnnotation annot : annots) {
                if (!(annot instanceof PdfLinkAnnotation)) continue;
                if (annot.getRectangle() == null) continue;
                PdfDictionary ad = annot.getPdfObject();
                String contents = null;
                PdfString existing = ad.getAsString(PdfName.Contents);
                if (existing != null) {
                    contents = existing.getValue();
                } else {
                    PdfDictionary action = ad.getAsDictionary(PdfName.A);
                    if (action != null) {
                        PdfString uri = action.getAsString(new PdfName("URI"));
                        if (uri != null) contents = uri.getValue();
                    }
                    if (contents == null && ad.get(PdfName.Dest) != null) {
                        contents = "Internal link";
                    }
                    if (contents == null) contents = "Link";
                }
                idx.computeIfAbsent(p, k -> new ArrayList<>())
                   .add(new LinkRect(annot.getRectangle().toRectangle(), contents, annot));
            }
        }
        return idx;
    }

    /** True if any of the element's runs falls inside a link annotation rect. */
    private static boolean anyRunCoveredByLink(List<RunRef> runs,
            Map<Long, float[]> runXY,
            Map<Integer, List<LinkRect>> pageLinkRects) {
        if (runs == null) return false;
        for (RunRef rr : runs) {
            if (linkForRun(rr.page, rr.runIdx, runXY, pageLinkRects) != null) {
                return true;
            }
        }
        return false;
    }

    /**
     * Find the link annotation whose rectangle contains a run's recorded
     * position (the run origin from Docling x/y, not a glyph-box center),
     * tested as a point within the rect ±tol. Returns null if the run has no
     * recorded position or no rect contains it.
     */
    private static LinkRect linkForRun(int page, int runIdx,
                                       Map<Long, float[]> runXY,
                                       Map<Integer, List<LinkRect>> pageLinkRects) {
        float[] xy = runXY.get(packRunKey(page, runIdx));
        if (xy == null) return null;
        List<LinkRect> rects = pageLinkRects.get(page);
        if (rects == null) return null;
        for (LinkRect lr : rects) {
            if (lr.contains(xy[0], xy[1], 2.0f)) return lr;
        }
        return null;
    }

    /**
     * Attach the link annotation as an OBJR kid of the Link struct element the
     * tag pointer is currently positioned on, and mark the LinkRect consumed.
     * PDF/UA 7.18.5: a link annotation must be nested in its Link structure
     * element. Idempotent per annotation via the consumed flag (caller checks).
     */
    private static void attachLinkObjr(TagTreePointer tagPointer,
            LinkRect lr, PdfDocument pdfDoc, PdfPage page) {
        tagPointer.setPageForTagging(page);
        // Empty registration MCR: getTagReference() + openTag/closeTag wires the
        // Link SE into the page's struct-parent tree so iText writes the OBJR's
        // /ParentTree entry (veraPDF 7.18.5). Mirrors tagLinkAnnotations.
        TagReference linkTagRef = tagPointer.getTagReference();
        PdfCanvas linkCanvas = getAppendCanvas(page, pdfDoc);
        linkCanvas.openTag(linkTagRef);
        linkCanvas.closeTag();
        PdfStructElem linkElem = tagPointer.getContext()
            .getPointerStructElem(tagPointer);
        int sp = pdfDoc.getNextStructParentIndex();
        lr.annot.getPdfObject().put(PdfName.StructParent, new PdfNumber(sp));
        lr.annot.setFlags(lr.annot.getFlags() | PdfAnnotation.PRINT);
        if (!lr.annot.getPdfObject().containsKey(PdfName.Contents)) {
            lr.annot.getPdfObject().put(PdfName.Contents,
                new PdfString(lr.contents));
        }
        linkElem.addKid(new PdfObjRef(lr.annot, linkElem, sp));
        lr.consumed = true;
    }

    private static void tagLinkAnnotations(PdfDocument pdfDoc,
                                            TagTreePointer tagPointer,
                                            Map<String, InlineLinkInfo> inlineLinkPaths) {
        int taggedLinks = 0;
        int inlineLinks = 0;

        try {
            tagPointer.moveToRoot();

            for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
                PdfPage page = pdfDoc.getPage(p);
                List<PdfAnnotation> annotations = page.getAnnotations();
                if (annotations == null || annotations.isEmpty()) continue;

                for (PdfAnnotation annot : annotations) {
                    if (!(annot instanceof PdfLinkAnnotation)) continue;

                    PdfDictionary annotDict = annot.getPdfObject();

                    // Skip already-tagged annotations
                    if (annotDict.containsKey(PdfName.StructParent)) continue;

                    try {
                        // Assign StructParent index
                        int structParentIndex = pdfDoc
                            .getNextStructParentIndex();
                        annotDict.put(PdfName.StructParent,
                            new PdfNumber(structParentIndex));

                        // Set PRINT flag
                        int flags = annot.getFlags();
                        annot.setFlags(flags | PdfAnnotation.PRINT);

                        // PDF/UA 7.18.1: /Contents with alternate description
                        String contentsText = null;
                        PdfString existingContents = annotDict.getAsString(
                            PdfName.Contents);
                        if (existingContents != null) {
                            // toUnicodeString() decodes UTF-16BE (BOM) source
                            // strings; getValue() returns raw bytes and, when
                            // re-encoded as UNICODE_BIG below, produces mojibake
                            // (e.g. a bepress cover-sheet UTF-16 /Contents came
                            // out as CJK gibberish in /ActualText).
                            contentsText = existingContents.toUnicodeString();
                        } else {
                            PdfDictionary action = annotDict
                                .getAsDictionary(PdfName.A);
                            if (action != null) {
                                PdfString uri = action.getAsString(
                                    new PdfName("URI"));
                                if (uri != null) {
                                    contentsText = uri.toUnicodeString();
                                }
                            }
                            if (contentsText == null) {
                                PdfObject dest = annotDict.get(PdfName.Dest);
                                if (dest != null) {
                                    contentsText = "Internal link";
                                }
                            }
                            if (contentsText == null) {
                                contentsText = "Link";
                            }
                            annotDict.put(PdfName.Contents,
                                new PdfString(contentsText));
                        }

                        // Check if an inline Link SE exists for this annotation
                        InlineLinkInfo inlineInfo = findInlineLinkPath(
                            contentsText, inlineLinkPaths);

                        if (inlineInfo != null) {
                            // Navigate to inline Link SE and add OBJR there
                            tagPointer.moveToRoot();
                            for (int idx : inlineInfo.treePath) {
                                tagPointer.moveToKid(idx);
                            }
                            tagPointer.setPageForTagging(page);

                            // Write empty MCR for ParentTree registration
                            TagReference linkTagRef =
                                tagPointer.getTagReference();
                            PdfCanvas linkCanvas =
                                getAppendCanvas(page, pdfDoc);
                            linkCanvas.openTag(linkTagRef);
                            linkCanvas.closeTag();

                            // Add OBJR kid
                            PdfStructElem linkElem = tagPointer.getContext()
                                .getPointerStructElem(tagPointer);
                            PdfObjRef objRef = new PdfObjRef(
                                annot, linkElem, structParentIndex);
                            linkElem.addKid(objRef);

                            tagPointer.moveToRoot();
                            taggedLinks++;
                            inlineLinks++;
                        } else {
                            // Fallback: create P > Link at Document root
                            tagPointer.setPageForTagging(page);
                            tagPointer.addTag("P");

                            PdfStructElem pWrapper = tagPointer.getContext()
                                .getPointerStructElem(tagPointer);
                            // Control chars (e.g. a leading \n from the source
                            // /Contents) are invalid in /ActualText — collapse
                            // to spaces and trim.
                            String pActual = contentsText
                                .replaceAll("[\\x00-\\x1F]", " ").trim();
                            pWrapper.setActualText(new PdfString(pActual,
                                PdfEncodings.UNICODE_BIG));

                            tagPointer.addTag("Link");

                            TagReference linkTagRef =
                                tagPointer.getTagReference();
                            PdfCanvas linkCanvas =
                                getAppendCanvas(page, pdfDoc);
                            linkCanvas.openTag(linkTagRef);
                            linkCanvas.closeTag();

                            PdfStructElem linkElem = tagPointer.getContext()
                                .getPointerStructElem(tagPointer);
                            PdfObjRef objRef = new PdfObjRef(
                                annot, linkElem, structParentIndex);
                            linkElem.addKid(objRef);

                            tagPointer.moveToParent(); // back to P
                            tagPointer.moveToParent(); // back to root
                            taggedLinks++;
                        }

                    } catch (Exception e) {
                        System.err.println("  Warning: failed to tag link "
                            + "on page " + p + ": " + e.getMessage());
                        try { tagPointer.moveToRoot(); }
                        catch (Exception ignored) {}
                    }
                }
            }
        } catch (Exception e) {
            System.err.println("Warning: link annotation tagging failed: "
                + e.getMessage());
        }

        // --- Synthesize link annotations for unmatched Docling URLs ---
        // Remaining entries in inlineLinkPaths are Link SEs with no
        // corresponding PDF annotation. Create PdfLinkAnnotation objects
        // so the Link SE has a proper OBJR kid (PAC compliance).
        int synthesized = 0;
        for (Map.Entry<String, InlineLinkInfo> entry
                : new ArrayList<>(inlineLinkPaths.entrySet())) {
            InlineLinkInfo linkInfo = entry.getValue();
            try {
                int pageNum = Math.min(linkInfo.pageNo,
                    pdfDoc.getNumberOfPages());
                PdfPage page = pdfDoc.getPage(pageNum);

                // Build annotation rectangle from parent P bbox
                float rectL, rectB, rectW, rectH;
                if (linkInfo.hasBbox) {
                    rectL = linkInfo.bboxL;
                    rectB = linkInfo.bboxB;
                    rectW = Math.max(linkInfo.bboxR - linkInfo.bboxL, 1f);
                    rectH = Math.max(linkInfo.bboxT - linkInfo.bboxB, 1f);
                } else {
                    rectL = 72f; rectB = 72f;
                    rectW = 1f;  rectH = 1f;
                }
                Rectangle linkRect = new Rectangle(
                    rectL, rectB, rectW, rectH);

                // Build URI — prepend http:// for www. URLs
                String uri = linkInfo.urlText != null
                    ? linkInfo.urlText.trim() : entry.getKey();
                if (uri.startsWith("www.")) {
                    uri = "http://" + uri;
                }

                // Create link annotation
                PdfLinkAnnotation linkAnnot =
                    new PdfLinkAnnotation(linkRect);
                PdfDictionary action = new PdfDictionary();
                action.put(PdfName.S, new PdfName("URI"));
                action.put(new PdfName("URI"), new PdfString(uri));
                linkAnnot.getPdfObject().put(PdfName.A, action);
                linkAnnot.setContents(new PdfString(uri));
                linkAnnot.setFlags(PdfAnnotation.PRINT);
                // Invisible border
                linkAnnot.getPdfObject().put(new PdfName("Border"),
                    new PdfArray(new float[]{0, 0, 0}));

                // Assign StructParent
                int structParentIndex = pdfDoc
                    .getNextStructParentIndex();
                linkAnnot.getPdfObject().put(PdfName.StructParent,
                    new PdfNumber(structParentIndex));

                // Add annotation to page WITHOUT iText auto-tagging.
                // page.addAnnotation() in tagged mode auto-creates an
                // OBJR at the current tag pointer position, which
                // conflicts with our manual OBJR below (duplicate
                // StructParent index).  Manually add to /Annots instead.
                linkAnnot.getPdfObject().makeIndirect(pdfDoc);
                linkAnnot.getPdfObject().put(PdfName.P,
                    page.getPdfObject());
                PdfArray annots = page.getPdfObject()
                    .getAsArray(PdfName.Annots);
                if (annots == null) {
                    annots = new PdfArray();
                    page.getPdfObject().put(PdfName.Annots, annots);
                }
                annots.add(linkAnnot.getPdfObject());

                // Navigate to the existing Link SE
                tagPointer.moveToRoot();
                for (int idx : linkInfo.treePath) {
                    tagPointer.moveToKid(idx);
                }
                tagPointer.setPageForTagging(page);

                // Write empty MCR for ParentTree registration
                TagReference linkTagRef =
                    tagPointer.getTagReference();
                PdfCanvas linkCanvas =
                    getAppendCanvas(page, pdfDoc);
                linkCanvas.openTag(linkTagRef);
                linkCanvas.closeTag();

                // Add OBJR kid to the Link SE
                PdfStructElem linkElem = tagPointer.getContext()
                    .getPointerStructElem(tagPointer);
                PdfObjRef objRef = new PdfObjRef(
                    linkAnnot, linkElem, structParentIndex);
                linkElem.addKid(objRef);

                tagPointer.moveToRoot();
                synthesized++;

            } catch (Exception e) {
                System.err.println("  Warning: failed to synthesize "
                    + "link annotation for '"
                    + entry.getKey() + "': " + e.getMessage());
                try { tagPointer.moveToRoot(); }
                catch (Exception ignored) {}
            }
        }

        if (taggedLinks > 0 || synthesized > 0) {
            System.out.println("  Tagged " + taggedLinks
                + " link annotations (OBJR-only)"
                + (inlineLinks > 0 ? ", " + inlineLinks + " inline" : "")
                + (synthesized > 0 ? ", " + synthesized
                    + " synthesized" : ""));
        }
    }

    // ---------------------------------------------------------------
    // Widget Annotation Tagging
    // ---------------------------------------------------------------

    /**
     * Find an inline Link SE path matching the given annotation URI/text.
     * Normalizes both sides (strip trailing punctuation, case-insensitive)
     * and tries substring matching. Returns null if no match found.
     * Removes matched entry from the map to prevent double-matching.
     */
    private static InlineLinkInfo findInlineLinkPath(
            String annotText, Map<String, InlineLinkInfo> inlineLinkPaths) {
        if (annotText == null || annotText.isEmpty()
                || inlineLinkPaths == null || inlineLinkPaths.isEmpty()) {
            return null;
        }
        String normAnnot = annotText.replaceAll("[;,.)\\s]+$", "")
            .trim().toLowerCase(java.util.Locale.ROOT);
        // Strip mailto: prefix for email links
        if (normAnnot.startsWith("mailto:")) {
            normAnnot = normAnnot.substring(7);
        }

        // Try exact match first
        for (Map.Entry<String, InlineLinkInfo> entry : inlineLinkPaths.entrySet()) {
            String normKey = entry.getKey().toLowerCase(java.util.Locale.ROOT);
            if (normKey.equals(normAnnot)
                    || normKey.contains(normAnnot)
                    || normAnnot.contains(normKey)) {
                InlineLinkInfo info = entry.getValue();
                inlineLinkPaths.remove(entry.getKey());
                return info;
            }
        }
        return null;
    }

    /**
     * Tag widget annotations in the structure tree (PDF/UA 7.18.4).
     * Creates Form structure elements with /Alt and OBJR kids.
     * Uses the same empty-MCR pattern as tagLinkAnnotations().
     */
    private static void tagWidgetAnnotations(PdfDocument pdfDoc,
                                              TagTreePointer tagPointer) {
        int taggedWidgets = 0;

        try {
            tagPointer.moveToRoot();

            for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
                PdfPage page = pdfDoc.getPage(p);
                List<PdfAnnotation> annotations = page.getAnnotations();
                if (annotations == null || annotations.isEmpty()) continue;

                for (PdfAnnotation annot : annotations) {
                    PdfDictionary annotDict = annot.getPdfObject();

                    // Only process Widget annotations
                    PdfName subtype = annotDict.getAsName(PdfName.Subtype);
                    if (subtype == null
                            || !"Widget".equals(subtype.getValue())) continue;

                    // Skip if already tagged (has /StructParent)
                    if (annotDict.containsKey(PdfName.StructParent)) continue;

                    try {
                        // Assign StructParent index
                        int structParentIndex = pdfDoc
                            .getNextStructParentIndex();
                        annotDict.put(PdfName.StructParent,
                            new PdfNumber(structParentIndex));

                        // Set PRINT flag
                        int flags = annot.getFlags();
                        annot.setFlags(flags | PdfAnnotation.PRINT);

                        // --- Build alt text ---
                        // Priority: /TU > /T > parent /TU > parent /T > fallback
                        String fieldName = null;
                        PdfString tu = annotDict.getAsString(
                            new PdfName("TU"));
                        if (tu != null && !tu.getValue().trim().isEmpty()) {
                            fieldName = tu.getValue().trim();
                        }
                        if (fieldName == null) {
                            PdfString t = annotDict.getAsString(
                                new PdfName("T"));
                            if (t != null && !t.getValue().trim().isEmpty()) {
                                fieldName = t.getValue().trim();
                            }
                        }
                        // Try parent field dict (merged fields)
                        PdfDictionary parent = annotDict.getAsDictionary(
                            PdfName.Parent);
                        if (fieldName == null && parent != null) {
                            tu = parent.getAsString(new PdfName("TU"));
                            if (tu != null
                                    && !tu.getValue().trim().isEmpty()) {
                                fieldName = tu.getValue().trim();
                            }
                            if (fieldName == null) {
                                PdfString t = parent.getAsString(
                                    new PdfName("T"));
                                if (t != null
                                        && !t.getValue().trim().isEmpty()) {
                                    fieldName = t.getValue().trim();
                                }
                            }
                        }
                        if (fieldName == null) {
                            fieldName = "Form field";
                        }

                        // Determine field type for screen reader prefix
                        // /FT may be on widget dict or parent field dict
                        String ft = null;
                        PdfName ftName = annotDict.getAsName(
                            new PdfName("FT"));
                        if (ftName != null) {
                            ft = ftName.getValue();
                        } else if (parent != null) {
                            ftName = parent.getAsName(new PdfName("FT"));
                            if (ftName != null) ft = ftName.getValue();
                        }

                        String altText;
                        if (ft == null) {
                            altText = fieldName;
                        } else if ("Btn".equals(ft)) {
                            // Distinguish checkbox vs radio via /Ff flags
                            // Bit 16 (0x10000) = radio, bit 17 (0x20000) = pushbutton
                            int ff = 0;
                            PdfNumber ffNum = annotDict.getAsNumber(
                                new PdfName("Ff"));
                            if (ffNum == null && parent != null) {
                                ffNum = parent.getAsNumber(
                                    new PdfName("Ff"));
                            }
                            if (ffNum != null) ff = ffNum.intValue();
                            if ((ff & 0x20000) != 0) {
                                altText = "Button: " + fieldName;
                            } else if ((ff & 0x10000) != 0) {
                                altText = "Radio button: " + fieldName;
                            } else {
                                altText = "Checkbox: " + fieldName;
                            }
                        } else if ("Tx".equals(ft)) {
                            altText = "Text field: " + fieldName;
                        } else if ("Ch".equals(ft)) {
                            altText = "Dropdown: " + fieldName;
                        } else if ("Sig".equals(ft)) {
                            altText = "Signature field: " + fieldName;
                        } else {
                            altText = fieldName;
                        }

                        // Create Form structure element
                        tagPointer.setPageForTagging(page);
                        tagPointer.addTag("Form");

                        PdfStructElem formElem = tagPointer.getContext()
                            .getPointerStructElem(tagPointer);
                        formElem.setAlt(new PdfString(altText,
                            PdfEncodings.UNICODE_BIG));

                        // Write empty MCR (BDC/EMC) for ParentTree registration.
                        // iText requires this to link the Form SE in the
                        // ParentTree.  The empty BDC/EMC bytes are stripped
                        // from content streams in stripEmptyFormBDC() after
                        // all tagging is complete.
                        TagReference formTagRef = tagPointer.getTagReference();
                        PdfCanvas formCanvas = getAppendCanvas(page, pdfDoc);
                        formCanvas.openTag(formTagRef);
                        formCanvas.closeTag();

                        // Add OBJR kid linking Form element to annotation
                        PdfObjRef objRef = new PdfObjRef(
                            annot, formElem, structParentIndex);
                        formElem.addKid(objRef);

                        // Remove MCR from /K, keeping only OBJR.
                        // PDF/UA 7.18.4 t2: Form must have exactly one
                        // child (the OBJR).
                        PdfObject kObj = formElem.getPdfObject()
                            .get(PdfName.K);
                        if (kObj instanceof PdfArray) {
                            PdfArray kArr = (PdfArray) kObj;
                            for (int ki = kArr.size() - 1; ki >= 0; ki--) {
                                PdfObject item = kArr.get(ki);
                                if (item instanceof PdfNumber) {
                                    kArr.remove(ki);
                                } else if (item instanceof PdfDictionary) {
                                    PdfDictionary d = (PdfDictionary) item;
                                    PdfName type = d.getAsName(PdfName.Type);
                                    if (type != null && "MCR".equals(
                                            type.getValue())) {
                                        kArr.remove(ki);
                                    }
                                }
                            }
                            if (kArr.size() == 1) {
                                formElem.getPdfObject().put(
                                    PdfName.K, kArr.get(0));
                            }
                        } else if (kObj instanceof PdfNumber) {
                            formElem.getPdfObject().remove(PdfName.K);
                        }

                        tagPointer.moveToParent(); // back to root
                        taggedWidgets++;

                    } catch (Exception e) {
                        System.err.println("  Warning: failed to tag widget "
                            + "on page " + p + ": " + e.getMessage());
                        try { tagPointer.moveToRoot(); }
                        catch (Exception ignored) {}
                    }
                }
            }
        } catch (Exception e) {
            System.err.println("Warning: widget annotation tagging failed: "
                + e.getMessage());
        }

        if (taggedWidgets > 0) {
            System.out.println("  Tagged " + taggedWidgets
                + " widget annotations in structure tree");
        }
    }

    // ---------------------------------------------------------------
    // Markup Annotation Tagging (PDF/UA 7.18.1)
    // ---------------------------------------------------------------

    /**
     * Tag markup annotations (FreeText, Square, Highlight, FileAttachment,
     * etc.) in the structure tree. PDF/UA 7.18.1 requires all visible
     * non-Widget, non-Link annotations to be nested within an Annot
     * structure element, and to have /Contents or /Alt (7.18.1 t2).
     *
     * Uses the same empty-MCR pattern as tagLinkAnnotations().
     */
    private static void tagMarkupAnnotations(PdfDocument pdfDoc,
                                              TagTreePointer tagPointer) {
        int taggedMarkup = 0;
        Set<String> MARKUP_SUBTYPES = new HashSet<>(Arrays.asList(
            "Text", "FreeText", "Square", "Circle", "Line", "Polygon",
            "PolyLine", "Highlight", "Underline", "StrikeOut", "Squiggly",
            "Ink", "Stamp", "Caret", "Redact", "FileAttachment",
            "Sound", "Movie", "Screen", "Watermark", "3D", "RichMedia",
            "Projection"
        ));

        try {
            tagPointer.moveToRoot();

            for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
                PdfPage page = pdfDoc.getPage(p);
                List<PdfAnnotation> annotations = page.getAnnotations();
                if (annotations == null || annotations.isEmpty()) continue;

                for (PdfAnnotation annot : annotations) {
                    PdfDictionary annotDict = annot.getPdfObject();

                    PdfName subtype = annotDict.getAsName(PdfName.Subtype);
                    if (subtype == null) continue;
                    String subtypeStr = subtype.getValue();
                    if (!MARKUP_SUBTYPES.contains(subtypeStr)) continue;

                    // Skip if already tagged
                    if (annotDict.containsKey(PdfName.StructParent)) continue;

                    // Skip hidden annotations (flag bit 1 = hidden)
                    int flags = annot.getFlags();
                    if ((flags & 2) != 0) continue;

                    try {
                        // Assign StructParent index
                        int structParentIndex = pdfDoc
                            .getNextStructParentIndex();
                        annotDict.put(PdfName.StructParent,
                            new PdfNumber(structParentIndex));

                        // Set Print flag
                        annot.setFlags(flags | PdfAnnotation.PRINT);

                        // Ensure /Contents key exists (required by 7.18.1 t2)
                        if (!annotDict.containsKey(PdfName.Contents)) {
                            String contentsText = null;

                            // FreeText: try /RC (rich text content)
                            if ("FreeText".equals(subtypeStr)) {
                                PdfString rc = annotDict.getAsString(
                                    new PdfName("RC"));
                                if (rc != null
                                        && !rc.toUnicodeString().trim().isEmpty()) {
                                    String raw = rc.toUnicodeString()
                                        .replaceAll("<[^>]+>", "").trim();
                                    if (!raw.isEmpty()) {
                                        contentsText = raw.length() > 200
                                            ? raw.substring(0, 200) : raw;
                                    }
                                }
                            }

                            // FileAttachment: try /FS /F (file name)
                            if (contentsText == null
                                    && "FileAttachment".equals(subtypeStr)) {
                                PdfDictionary fs = annotDict
                                    .getAsDictionary(new PdfName("FS"));
                                if (fs != null) {
                                    PdfString f = fs.getAsString(PdfName.F);
                                    if (f != null
                                            && !f.toUnicodeString().trim().isEmpty()) {
                                        contentsText = "File attachment: "
                                            + f.toUnicodeString().trim();
                                    }
                                }
                            }

                            // Try /Subj (subject)
                            if (contentsText == null) {
                                PdfString subj = annotDict.getAsString(
                                    new PdfName("Subj"));
                                if (subj != null
                                        && !subj.toUnicodeString().trim().isEmpty()) {
                                    contentsText = subj.toUnicodeString().trim();
                                }
                            }

                            // Try /T (title/author)
                            if (contentsText == null) {
                                PdfString t = annotDict.getAsString(
                                    new PdfName("T"));
                                if (t != null
                                        && !t.toUnicodeString().trim().isEmpty()) {
                                    contentsText = t.toUnicodeString().trim();
                                }
                            }

                            // Fallback
                            if (contentsText == null) {
                                contentsText = subtypeStr + " annotation";
                            }

                            annotDict.put(PdfName.Contents,
                                new PdfString(contentsText,
                                    PdfEncodings.UNICODE_BIG));
                        }

                        // Create Annot structure element (direct child of root)
                        tagPointer.setPageForTagging(page);
                        tagPointer.addTag("Annot");

                        PdfStructElem annotElem = tagPointer.getContext()
                            .getPointerStructElem(tagPointer);

                        // Set /Alt on the Annot SE (7.18.1 t2 fallback)
                        String altText = annotDict.containsKey(PdfName.Contents)
                            ? annotDict.getAsString(PdfName.Contents).getValue()
                            : subtypeStr + " annotation";
                        annotElem.setAlt(new PdfString(altText,
                            PdfEncodings.UNICODE_BIG));

                        // Write empty MCR (BDC/EMC) for ParentTree registration
                        TagReference annotTagRef =
                            tagPointer.getTagReference();
                        PdfCanvas annotCanvas =
                            getAppendCanvas(page, pdfDoc);
                        annotCanvas.openTag(annotTagRef);
                        annotCanvas.closeTag();

                        // Add OBJR kid linking Annot element to annotation
                        PdfObjRef objRef = new PdfObjRef(
                            annot, annotElem, structParentIndex);
                        annotElem.addKid(objRef);

                        tagPointer.moveToParent(); // back to root
                        taggedMarkup++;

                    } catch (Exception e) {
                        System.err.println("  Warning: failed to tag "
                            + subtypeStr + " annotation on page " + p
                            + ": " + e.getMessage());
                        try { tagPointer.moveToRoot(); }
                        catch (Exception ignored) {}
                    }
                }
            }
        } catch (Exception e) {
            System.err.println("Warning: markup annotation tagging failed: "
                + e.getMessage());
        }

        if (taggedMarkup > 0) {
            System.out.println("  Tagged " + taggedMarkup
                + " markup annotation(s) in structure tree");
        }
    }

    /**
     * Get a PdfCanvas that appends to a page's content stream WITHOUT
     * wrapping existing content in q/Q graphics state save/restore.
     * The 1-arg PdfCanvas(page) constructor adds q/Q to isolate new
     * content from existing, but empty MCR markers (BDC/EMC) have no
     * visible content and don't need graphics state isolation.
     * Using this avoids introducing spurious q/Q imbalances.
     */
    static PdfCanvas getAppendCanvas(PdfPage page,
                                     PdfDocument pdfDoc) {
        PdfStream stream = page.getLastContentStream();
        if (stream == null || stream.getOutputStream() == null) {
            stream = page.newContentStreamAfter();
        }
        return new PdfCanvas(stream, page.getResources(), pdfDoc);
    }

    static int getHeadingLevel(String tag) {
        if (tag == null || !tag.startsWith("H")) return 0;
        try { return Integer.parseInt(tag.substring(1)); }
        catch (NumberFormatException e) { return 0; }
    }

    // ---------------------------------------------------------------
    // Data classes
    // ---------------------------------------------------------------

    /**
     * Embed a formula's MathML as an Associated File (/AF, AFRelationship
     * /Supplement) on its Formula structure element. Additive to the /Alt
     * caption; a null/empty MathML is a no-op. Never aborts tagging on error.
     */
    static void embedMathmlAF(PdfDocument pdfDoc, PdfStructElem se,
                              ElementInfo info) {
        if (info.mathml == null || info.mathml.trim().isEmpty()) return;
        try {
            byte[] bytes = info.mathml.getBytes(StandardCharsets.UTF_8);
            String safeId = (info.id != null && !info.id.isEmpty())
                ? info.id.replaceAll("[^A-Za-z0-9_-]", "_")
                : ("p" + info.pageNo);
            PdfFileSpec fs = PdfFileSpec.createEmbeddedFileSpec(
                pdfDoc, bytes,
                "MathML representation of formula",
                "formula_" + safeId + ".mathml",
                new PdfName("application/mathml+xml"),
                null,
                new PdfName("Supplement"));
            se.addAssociatedFile(fs);
        } catch (Exception e) {
            System.out.println("  WARN: MathML AF embed failed for formula "
                + info.id + ": " + e.getMessage());
        }
    }

    static class ElementInfo {
        final String id, tag, text, altText;
        final int pageNo;
        String mathml;              // standalone MathML doc for Formula AF, or null
        String caption;             // Table caption (Docling/VLM), null if absent
        float bboxL, bboxB, bboxR, bboxT;
        boolean hasBbox;
        List<CellInfo> cells;       // non-null only for Table elements
        List<ListItemInfo> listItems; // non-null only for L elements
        List<TocItemInfo> tocItems;   // non-null only for TOC elements
        List<ParagraphChildInfo> paragraphChildren; // non-null for P with Link children
        List<RunRef> parentRunRefs;  // non-null for any P with content runs (Step 5b)
        List<Object[]> geoLinkSegments; // {LinkRect|null, String text, List<Long> runKeys} or null

        ElementInfo(String id, String tag, String text, String altText,
                     int pageNo) {
            this.id = id; this.tag = tag; this.text = text;
            this.altText = altText; this.pageNo = pageNo;
        }
    }

    /** A reference to one analyzed text run, with its source text. */
    static class RunRef {
        final int page;
        final int runIdx;
        final String text;
        RunRef(int page, int runIdx, String text) {
            this.page = page;
            this.runIdx = runIdx;
            this.text = text == null ? "" : text;
        }
    }

    /** A PDF link annotation with the data needed to build a Link SE. */
    static class LinkRect {
        final float l, b, r, t;          // rectangle in PDF user space
        final String contents;            // /Contents (URI, "Internal link", or "Link")
        final PdfAnnotation annot;        // the source annotation (for OBJR)
        boolean consumed = false;         // set true once a Link SE owns it

        LinkRect(Rectangle rect, String contents, PdfAnnotation annot) {
            this.l = rect.getX();
            this.b = rect.getY();
            this.r = rect.getX() + rect.getWidth();
            this.t = rect.getY() + rect.getHeight();
            this.contents = contents;
            this.annot = annot;
        }

        boolean contains(float x, float y, float tol) {
            return x >= l - tol && x <= r + tol && y >= b - tol && y <= t + tol;
        }
    }

    static class ParagraphChildInfo {
        final String tag;   // "Span" or "Link"
        final String text;

        ParagraphChildInfo(String tag, String text) {
            this.tag = tag;
            this.text = text;
        }
    }

    /** Bundles tree path + page + bbox + URL text for inline Link SEs. */
    static class InlineLinkInfo {
        final int[] treePath;
        final int pageNo;
        final float bboxL, bboxB, bboxR, bboxT;
        final boolean hasBbox;
        final String urlText;

        InlineLinkInfo(int[] treePath, int pageNo,
                       float bboxL, float bboxB, float bboxR, float bboxT,
                       boolean hasBbox, String urlText) {
            this.treePath = treePath;
            this.pageNo = pageNo;
            this.bboxL = bboxL; this.bboxB = bboxB;
            this.bboxR = bboxR; this.bboxT = bboxT;
            this.hasBbox = hasBbox;
            this.urlText = urlText;
        }
    }

    static class ListItemInfo {
        final String text, label;
        final List<int[]> runRefs;
        String liId;
        float bboxL, bboxB, bboxR, bboxT;
        boolean hasBbox;

        ListItemInfo(String text, String label, List<int[]> runRefs) {
            this.text = text;
            this.label = label;
            this.runRefs = runRefs;
        }
    }

    static class TocItemInfo {
        final String text;
        final List<int[]> runRefs;  // each: {page, runIndex}
        String tociId;              // generated: "elemId_toci0"
        float[] bbox;               // [l, b, r, t] or null

        TocItemInfo(String text, List<int[]> runRefs) {
            this.text = text;
            this.runRefs = runRefs;
        }
    }

    static class CellInfo {
        final int row, col;
        int rowSpan, colSpan;  // mutable for span clipping
        final String text;
        final boolean isHeader, isRowHeader;
        boolean isColHeader;  // non-final: first-row-TH fallback promotes
        final List<int[]> runRefs;  // each: {page, runIndex}
        final int pageNo;
        String cellId;  // generated: "elemId_r0c1"
        float bboxL, bboxB, bboxR, bboxT;
        boolean hasBbox;

        CellInfo(int row, int col, int rowSpan, int colSpan,
                 String text, boolean isHeader, boolean isColHeader,
                 boolean isRowHeader, List<int[]> runRefs, int pageNo) {
            this.row = row; this.col = col;
            this.rowSpan = rowSpan; this.colSpan = colSpan;
            this.text = text;
            this.isHeader = isHeader;
            this.isColHeader = isColHeader;
            this.isRowHeader = isRowHeader;
            this.runRefs = runRefs;
            this.pageNo = pageNo;
        }
    }
}
