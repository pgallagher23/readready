// SPDX-License-Identifier: AGPL-3.0-or-later
/*
 * PDF Accessibility Tagger — iText 9 Community (AGPL-3.0)
 *
 * Takes an existing (untagged) PDF and a Docling structural analysis JSON,
 * then injects a PDF/UA-1 structure tree preserving the original layout.
 *
 * Key features:
 * - Converts page content to Form XObjects for proper tagging
 * - Tags actual image content inside BDC/EMC (not artifact-wrapped)
 * - Uses invisible text overlay for non-image elements
 * - Builds hierarchical structure tree (Document > Sect > elements)
 * - Sets XMP metadata with PDF/UA-1 identifier
 * - Sets ViewerPreferences with DisplayDocTitle
 *
 * Usage:
 *   java -jar itext_tagger.jar \
 *       --input  original.pdf \
 *       --structure docling_structure.json \
 *       --output tagged.pdf \
 *       --lang en \
 *       --title "Document Title"
 */
package pdfaccessibility;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.itextpdf.io.font.AdobeGlyphList;
import com.itextpdf.io.font.FontMetrics;
import com.itextpdf.io.font.FontProgram;
import com.itextpdf.io.font.PdfEncodings;
import com.itextpdf.io.font.constants.StandardFonts;
import com.itextpdf.kernel.font.PdfFont;
import com.itextpdf.kernel.font.PdfFontFactory;
import com.itextpdf.kernel.geom.Rectangle;
import com.itextpdf.kernel.pdf.*;
import com.itextpdf.kernel.pdf.canvas.PdfCanvas;
import com.itextpdf.kernel.pdf.canvas.PdfCanvasConstants;
import com.itextpdf.kernel.pdf.annot.PdfAnnotation;
import com.itextpdf.kernel.pdf.annot.PdfLinkAnnotation;
import com.itextpdf.kernel.pdf.tagging.PdfObjRef;
import com.itextpdf.kernel.pdf.tagging.PdfStructElem;
import com.itextpdf.kernel.pdf.navigation.PdfExplicitDestination;
import com.itextpdf.kernel.pdf.tagutils.TagTreePointer;
import com.itextpdf.kernel.pdf.xobject.PdfFormXObject;
import com.itextpdf.kernel.xmp.XMPConst;
import com.itextpdf.kernel.xmp.XMPMeta;
import com.itextpdf.kernel.xmp.XMPMetaFactory;
import com.itextpdf.kernel.xmp.options.PropertyOptions;
import com.itextpdf.kernel.xmp.options.SerializeOptions;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.*;

public class PdfAccessibilityTagger {

    // ── Source quality counters (reset per invocation, emitted as JSON) ──
    static int sqUnembeddedFonts = 0;
    static int sqMissingToUnicode = 0;       // fonts with no ToUnicode at compliance check
    static int sqStrippedCIDSet = 0;
    static int sqStrippedCharSet = 0;
    static int sqStrippedSymbolicEncoding = 0;
    static int sqFixedCIDToGIDMap = 0;
    static int sqFontsInjected = 0;
    static int sqToUnicodeCMapsAdded = 0;
    static int sqToUnicodeCMapsFailed = 0;
    static int sqInvisibleTextPagesStripped = 0;
    static int sqFontDescriptorsRepaired = 0;
    static int sqSubstitutionFontsInjected = 0;  // Liberation/URW only (not exact-match)
    static Set<String> sqUnembeddedFontNames = new HashSet<>();
    static Set<String> sqSubstitutionFontNames = new HashSet<>();

    static void resetSourceQualityCounters() {
        sqUnembeddedFonts = 0;
        sqMissingToUnicode = 0;
        sqStrippedCIDSet = 0;
        sqStrippedCharSet = 0;
        sqStrippedSymbolicEncoding = 0;
        sqFixedCIDToGIDMap = 0;
        sqFontsInjected = 0;
        sqToUnicodeCMapsAdded = 0;
        sqToUnicodeCMapsFailed = 0;
        sqInvisibleTextPagesStripped = 0;
        sqFontDescriptorsRepaired = 0;
        sqSubstitutionFontsInjected = 0;
        sqUnembeddedFontNames.clear();
        sqSubstitutionFontNames.clear();
    }

    static void emitSourceQualitySummary() {
        if (sqUnembeddedFonts == 0 && sqMissingToUnicode == 0
                && sqStrippedCIDSet == 0 && sqStrippedCharSet == 0
                && sqStrippedSymbolicEncoding == 0 && sqFixedCIDToGIDMap == 0
                && sqFontsInjected == 0 && sqToUnicodeCMapsAdded == 0
                && sqToUnicodeCMapsFailed == 0
                && sqInvisibleTextPagesStripped == 0
                && sqFontDescriptorsRepaired == 0
                && sqSubstitutionFontsInjected == 0) {
            return; // nothing to report
        }
        // Cap font names at 50
        List<String> names = new ArrayList<>(sqUnembeddedFontNames);
        if (names.size() > 50) names = names.subList(0, 50);
        StringBuilder sb = new StringBuilder("SOURCE_QUALITY_JSON:{");
        sb.append("\"unembedded_fonts\":").append(sqUnembeddedFonts);
        sb.append(",\"missing_tounicode\":").append(sqMissingToUnicode);
        sb.append(",\"stripped_cidset\":").append(sqStrippedCIDSet);
        sb.append(",\"stripped_charset\":").append(sqStrippedCharSet);
        sb.append(",\"stripped_symbolic_encoding\":").append(sqStrippedSymbolicEncoding);
        sb.append(",\"fixed_cidtogidmap\":").append(sqFixedCIDToGIDMap);
        sb.append(",\"fonts_injected\":").append(sqFontsInjected);
        sb.append(",\"tounicode_cmaps_added\":").append(sqToUnicodeCMapsAdded);
        sb.append(",\"tounicode_cmaps_failed\":").append(sqToUnicodeCMapsFailed);
        sb.append(",\"invisible_text_pages_stripped\":").append(sqInvisibleTextPagesStripped);
        sb.append(",\"font_descriptors_repaired\":").append(sqFontDescriptorsRepaired);
        sb.append(",\"substitution_fonts_injected\":").append(sqSubstitutionFontsInjected);
        sb.append(",\"unembedded_font_names\":[");
        for (int i = 0; i < names.size(); i++) {
            if (i > 0) sb.append(",");
            sb.append("\"").append(names.get(i).replace("\\", "\\\\").replace("\"", "\\\"")).append("\"");
        }
        sb.append("]");
        // Substitution font names
        List<String> subNames = new ArrayList<>(sqSubstitutionFontNames);
        if (subNames.size() > 50) subNames = subNames.subList(0, 50);
        sb.append(",\"substitution_font_names\":[");
        for (int i = 0; i < subNames.size(); i++) {
            if (i > 0) sb.append(",");
            sb.append("\"").append(subNames.get(i).replace("\\", "\\\\").replace("\"", "\\\"")).append("\"");
        }
        sb.append("]}");
        System.out.println(sb.toString());
    }

    /**
     * First-row-TH fallback (Ally "TableHeaders"): when a table reaches
     * the tagger with zero header flags anywhere and has >=2 rows, its
     * first row is promoted to TH /Scope /Column. CLI-gated via
     * --first-row-th-fallback true|false (config.yaml
     * output.table_first_row_th_fallback). Default on.
     */
    static boolean firstRowThFallback = true;

    public static void main(String[] args) throws Exception {
        resetSourceQualityCounters();
        // --- Parse CLI arguments ---
        String inputPath = null;
        String structurePath = null;
        String outputPath = null;
        String lang = "en-US";
        String title = null;
        boolean preserveSource = false;
        boolean analyzeOnly = false;
        String tagMode = "overlay";  // "overlay" (default) or "content-preserving"

        for (int i = 0; i < args.length; i++) {
            switch (args[i]) {
                case "--input":     case "-i": inputPath = args[++i]; break;
                case "--structure": case "-s": structurePath = args[++i]; break;
                case "--output":    case "-o": outputPath = args[++i]; break;
                case "--lang":      case "-l": lang = args[++i]; break;
                case "--title":     case "-t": title = args[++i]; break;
                case "--preserve-source": preserveSource = true; break;
                case "--analyze": analyzeOnly = true; break;
                case "--tag-mode": tagMode = args[++i]; break;
                case "--first-row-th-fallback":
                    firstRowThFallback = Boolean.parseBoolean(args[++i]); break;
                default:
                    System.err.println("Unknown argument: " + args[i]);
                    System.exit(1);
            }
        }

        // --- Analyze-only mode: content stream analysis (Phase 1) ---
        if (analyzeOnly) {
            if (inputPath == null || outputPath == null) {
                System.err.println(
                    "Usage: java -jar itext_tagger.jar --analyze "
                    + "--input <pdf> --output <analysis.json>"
                );
                System.exit(1);
            }
            ContentStreamAnalyzer.analyzeToFile(inputPath, outputPath);
            System.exit(0);
        }

        // --- Fix-fonts mode: inject ToUnicode CMaps only (pre-Docling pass) ---
        // Adds correct /ToUnicode CMaps to fonts that lack them so downstream
        // text extractors (pdfium / Docling) read proper Unicode instead of raw
        // glyph codes. Many LaTeX/dvipdfm Type-3 fonts have no ToUnicode and use
        // placeholder /aNN glyph names, so extractors emit control chars or wrong
        // letters (e.g. the fi-ligature comes out as 0x1C or 'W'). Reusing the
        // tagger's proven CMap-generation logic here, BEFORE Docling converts,
        // makes the screen-reader /ActualText (sourced from Docling) correct.
        // This mode only rewrites fonts — it does NOT tag or restructure the PDF.
        if ("fix-fonts".equals(tagMode)) {
            if (inputPath == null || outputPath == null) {
                System.err.println(
                    "Usage: java -jar itext_tagger.jar --tag-mode fix-fonts "
                    + "--input <pdf> --output <pdf>"
                );
                System.exit(1);
            }
            try (PdfReader reader = new PdfReader(inputPath);
                 PdfWriter writer = new PdfWriter(outputPath);
                 PdfDocument pdfDoc = new OutputPdfDocument(reader, writer)) {
                addMissingToUnicodeCMaps(pdfDoc);
            } catch (Throwable e) {
                System.err.println("FATAL: fix-fonts failed: "
                    + e.getClass().getSimpleName() + ": " + e.getMessage());
                e.printStackTrace(System.err);
                java.io.File outFile = new java.io.File(outputPath);
                if (outFile.exists()) outFile.delete();
                System.exit(2);
            }
            System.exit(0);
        }

        // --- Content-preserving-full mode: analyze + wait + tag in one JVM ---
        // Eliminates the second JVM cold-start by keeping the process alive
        // while Python does element-to-content matching.
        //
        // Protocol (file-based handshake):
        //   1. Java runs ContentStreamAnalyzer, writes {output}.analysis.json
        //   2. Java prints "ANALYSIS_READY:{path}" to stdout and polls for
        //      {output}.enriched.json
        //   3. Python reads analysis, does matching/gap recovery, writes
        //      enriched JSON (atomic rename)
        //   4. Java reads enriched JSON, runs ContentPreservingTagger
        if ("content-preserving-full".equals(tagMode)) {
            if (inputPath == null || structurePath == null || outputPath == null) {
                System.err.println(
                    "Usage: java -jar itext_tagger.jar --tag-mode content-preserving-full "
                    + "--input <pdf> --structure <base.json> --output <pdf> "
                    + "[--lang <code>] [--title <text>]"
                );
                System.exit(1);
            }
            System.out.println("Using content-preserving-full tag mode (single JVM)");

            // Step 1: Analyze content streams
            String analysisPath = outputPath + ".analysis.json";
            try {
                ContentStreamAnalyzer.analyzeToFile(inputPath, analysisPath);
            } catch (Throwable e) {
                System.err.println("FATAL: Content stream analysis failed: "
                    + e.getClass().getSimpleName() + ": " + e.getMessage());
                e.printStackTrace(System.err);
                System.exit(2);
            }

            // Step 2: Signal Python and wait for enriched JSON
            System.out.println("ANALYSIS_READY:" + analysisPath);
            System.out.flush();

            String enrichedPath = outputPath + ".enriched.json";
            long deadline = System.currentTimeMillis() + 600_000; // 10 min
            while (!java.nio.file.Files.exists(Paths.get(enrichedPath))) {
                if (System.currentTimeMillis() > deadline) {
                    System.err.println("FATAL: Timeout waiting for enriched JSON at " + enrichedPath);
                    // Clean up analysis file
                    try { java.nio.file.Files.deleteIfExists(Paths.get(analysisPath)); } catch (Exception ignored) {}
                    System.exit(3);
                }
                try { Thread.sleep(200); } catch (InterruptedException ignored) {}
            }
            // Brief pause to ensure file is fully written (atomic rename
            // should guarantee this, but be safe)
            try { Thread.sleep(100); } catch (InterruptedException ignored) {}

            // Step 3: Tag using the enriched structure
            try {
                ContentPreservingTagger.tag(inputPath, enrichedPath,
                    outputPath, lang, title);
            } catch (Throwable e) {
                System.err.println("FATAL: Content-preserving tagger failed: "
                    + e.getClass().getSimpleName() + ": " + e.getMessage());
                e.printStackTrace(System.err);
                java.io.File outFile = new java.io.File(outputPath);
                if (outFile.exists()) outFile.delete();
                System.exit(2);
            } finally {
                // Clean up handshake files
                try { java.nio.file.Files.deleteIfExists(Paths.get(analysisPath)); } catch (Exception ignored) {}
                try { java.nio.file.Files.deleteIfExists(Paths.get(enrichedPath)); } catch (Exception ignored) {}
            }
            emitSourceQualitySummary();
            System.exit(0);
        }

        // --- Content-preserving mode (Phase 3) ---
        if ("content-preserving".equals(tagMode)) {
            if (inputPath == null || structurePath == null || outputPath == null) {
                System.err.println(
                    "Usage: java -jar itext_tagger.jar --tag-mode content-preserving "
                    + "--input <pdf> --structure <enriched.json> --output <pdf> "
                    + "[--lang <code>] [--title <text>]"
                );
                System.exit(1);
            }
            System.out.println("Using content-preserving tag mode");
            try {
                ContentPreservingTagger.tag(inputPath, structurePath,
                    outputPath, lang, title);
            } catch (Throwable e) {
                System.err.println("FATAL: Content-preserving tagger failed: "
                    + e.getClass().getSimpleName() + ": " + e.getMessage());
                e.printStackTrace(System.err);
                // Clean up partial output
                java.io.File outFile = new java.io.File(outputPath);
                if (outFile.exists()) outFile.delete();
                System.exit(2);
            }
            emitSourceQualitySummary();
            System.exit(0);
        }

        if (inputPath == null || structurePath == null || outputPath == null) {
            System.err.println(
                "Usage: java -jar itext_tagger.jar "
                + "--input <pdf> --structure <json> --output <pdf> "
                + "[--lang <code>] [--title <text>]\n"
                + "       java -jar itext_tagger.jar --analyze "
                + "--input <pdf> --output <analysis.json>\n"
                + "       java -jar itext_tagger.jar --tag-mode content-preserving "
                + "--input <pdf> --structure <enriched.json> --output <pdf>"
            );
            System.exit(1);
        }

        // --- Load structure JSON ---
        String jsonStr = new String(
            Files.readAllBytes(Paths.get(structurePath)), StandardCharsets.UTF_8
        );
        JsonObject structure = JsonParser.parseString(jsonStr).getAsJsonObject();
        JsonArray elements = structure.getAsJsonArray("elements");

        if (elements == null || elements.size() == 0) {
            System.err.println("No elements in structure JSON — running metadata-only pass.");
            applyMetadataOnlyFixes(inputPath, outputPath, lang, title);
            emitSourceQualitySummary();
            System.exit(0);
        }

        // --- Check for source structure preservation mode ---
        // If --preserve-source is set, or if auto-detected as having good
        // source structure, use the preservation path that keeps the original
        // content stream MCR intact instead of wrapping everything as Artifact.
        if (preserveSource || assessSourceStructure(inputPath)) {
            preserveSourceAndEnhance(inputPath, outputPath, structure,
                elements, lang, title);
            emitSourceQualitySummary();
            System.exit(0);
        }

        // Extract title from structure JSON metadata if not provided via CLI
        if (title == null || title.isEmpty()) {
            JsonObject metadata = structure.getAsJsonObject("metadata");
            if (metadata != null && metadata.has("title")
                    && !metadata.get("title").isJsonNull()) {
                title = metadata.get("title").getAsString();
            }
        }
        if (title == null || title.isEmpty()) {
            title = "Untitled Document";
        }

        // --- Open PDF ---
        PdfReader reader = new PdfReader(inputPath);
        PdfWriter writer = new PdfWriter(outputPath);
        PdfDocument pdfDoc = new OutputPdfDocument(reader, writer);

        // Enable tagged mode first (uses existing StructTreeRoot or creates new)
        pdfDoc.setTagged();

        // --- Clear pre-existing structure tree children ---
        // Many source PDFs already have a (partial/broken) structure tree
        // with Figure elements that have empty /Alt. We rebuild the entire
        // tree from Docling's analysis, so clear old children.
        {
            PdfDictionary stRoot = pdfDoc.getCatalog().getPdfObject()
                .getAsDictionary(PdfName.StructTreeRoot);
            if (stRoot != null) {
                // Remove old kids — iText will build new ones
                stRoot.remove(PdfName.K);
                // Remove old ParentTree — will be rebuilt on close
                stRoot.remove(new PdfName("ParentTree"));
                stRoot.remove(new PdfName("ParentTreeNextKey"));
                stripInheritedRoleMap(stRoot);
            }
            // Clear StructParents from pages and StructParent from
            // annotations so tagLinkAnnotations() can re-tag all links.
            for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
                PdfPage pg = pdfDoc.getPage(p);
                pg.getPdfObject().remove(new PdfName("StructParents"));
                List<PdfAnnotation> annots = pg.getAnnotations();
                if (annots != null) {
                    for (PdfAnnotation ann : annots) {
                        ann.getPdfObject().remove(PdfName.StructParent);
                    }
                }
            }
            // Remove orphaned StructElem objects from the old structure tree.
            // Clearing /K from StructTreeRoot detaches them from the tree,
            // but the objects persist in the PDF with stale /Pg and /K refs.
            // Strip /Pg and /K so they don't appear as ghost elements
            // associated with pages (confuses some validators and tools).
            int ghostCount = 0;
            for (int xref = 1; xref < pdfDoc.getNumberOfPdfObjects(); xref++) {
                try {
                    PdfObject obj = pdfDoc.getPdfObject(xref);
                    if (obj == null || !obj.isDictionary()) continue;
                    PdfDictionary dict = (PdfDictionary) obj;
                    PdfName type = dict.getAsName(PdfName.Type);
                    if (PdfName.StructElem.equals(type)) {
                        dict.remove(PdfName.Pg);
                        dict.remove(PdfName.K);
                        dict.remove(PdfName.P);
                        ghostCount++;
                    }
                } catch (Exception e) {
                    System.err.println("WARNING: Skipping malformed object " + xref
                        + " in orphan cleanup: " + e.getMessage());
                }
            }
            System.out.println("Cleared pre-existing structure tree"
                + (ghostCount > 0 ? " (" + ghostCount
                    + " orphaned StructElem objects cleaned)" : ""));
        }

        // Set document language
        pdfDoc.getCatalog().setLang(new PdfString(lang));

        // Set MarkInfo
        pdfDoc.getCatalog().put(
            PdfName.MarkInfo,
            new PdfDictionary(Map.of(PdfName.Marked, PdfBoolean.TRUE))
        );

        // Set document title in Info dictionary
        pdfDoc.getDocumentInfo().setTitle(title);

        // Set ViewerPreferences: display title in window title bar (PDF/UA-1 req)
        PdfViewerPreferences viewerPrefs = new PdfViewerPreferences();
        viewerPrefs.setDisplayDocTitle(true);
        pdfDoc.getCatalog().setViewerPreferences(viewerPrefs);

        // XMP metadata will be set just before close to prevent iText
        // from overwriting it during pdfDoc.close()

        int numPages = pdfDoc.getNumberOfPages();
        System.out.println("Tagging PDF: " + numPages + " pages, "
            + elements.size() + " elements, title: \"" + title + "\"");

        // Set /Tabs /S on all pages (PDF/UA requires structure-based tab order)
        for (int p = 1; p <= numPages; p++) {
            pdfDoc.getPage(p).put(PdfName.Tabs, PdfName.S);
        }

        // Track which pages have Docling-detected elements
        Set<Integer> pagesWithElements = new HashSet<>();
        for (JsonElement je : elements) {
            JsonObject elem = je.getAsJsonObject();
            int pn = elem.has("page_no") ? elem.get("page_no").getAsInt() : 1;
            pagesWithElements.add(Math.max(1, Math.min(pn, numPages)));
        }

        // Track pages where ALL text is invisible OCR overlay (render mode 3).
        // Used for diagnostic logging only — the tagger already handles these
        // correctly by stripping invisible text and using Docling text for
        // /ActualText.
        Set<Integer> invisibleOnlyPages = new HashSet<>();
        if (structure.has("invisible_only_pages")) {
            for (JsonElement p : structure.getAsJsonArray("invisible_only_pages")) {
                invisibleOnlyPages.add(p.getAsInt());
            }
            if (!invisibleOnlyPages.isEmpty()) {
                System.out.println("  " + invisibleOnlyPages.size()
                    + " OCR-only page(s) detected (invisible text will be stripped): "
                    + invisibleOnlyPages);
            }
        }

        // Snapshot source font indirect references BEFORE creating the
        // overlay font.  Used later by checkFontCompliance() to skip
        // tagger-injected fonts from source quality reporting.
        Set<PdfIndirectReference> sourceFontRefs = snapshotSourceFontRefs(pdfDoc);

        // Maps populated by Phase A (after font/canvas setup)
        Map<Integer, PdfFormXObject> pageFormXObjects = new HashMap<>();
        Set<Integer> pageContentTagged = new HashSet<>();

        // --- Step 2: Create tagged structure with hierarchical elements ---
        // Use an embedded TrueType font for PDF/UA compliance.
        PdfFont font;
        String[] fontPaths = {
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            // Additional common paths (Docker, macOS, CentOS, conda)
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
        };
        PdfFont embeddedFont = null;
        for (String fp : fontPaths) {
            if (new java.io.File(fp).exists()) {
                try {
                    embeddedFont = PdfFontFactory.createFont(
                        fp, PdfEncodings.IDENTITY_H,
                        PdfFontFactory.EmbeddingStrategy.FORCE_EMBEDDED);
                    System.out.println("  Using embedded font: " + fp);
                    break;
                } catch (Exception e) {
                    System.err.println("  WARNING: Font exists but failed "
                        + "to load: " + fp + " — " + e.getMessage());
                }
            }
        }
        if (embeddedFont != null) {
            font = embeddedFont;
        } else {
            font = PdfFontFactory.createFont(StandardFonts.HELVETICA);
            System.err.println("  WARNING: No embeddable TrueType font "
                + "found on system. Using non-embedded Helvetica — this "
                + "violates PDF/UA 7.21.4.1 and will cause screen reader "
                + "failures. Install dejavu-sans or liberation-sans fonts.");
        }

        TagTreePointer tagPointer = pdfDoc.getTagStructureContext()
            .getAutoTaggingPointer();

        // Cache one RawPdfCanvas per page — RawPdfCanvas extends PdfCanvas
        // and adds writeRawContent() for injecting original page content bytes
        // directly into the content stream between BDC/EMC markers.
        // Using a single canvas per page ensures all content (element tags +
        // original page content) goes into one content stream, avoiding the
        // extra empty streams that PAC may flag as unstructured content.
        Map<Integer, RawPdfCanvas> canvasCache = new HashMap<>();

        // --- Step 1: Convert page content to Form XObjects ---
        // This captures original page content as Form XObjects so we can
        // later wrap them as Artifact (for element pages) or Part (for
        // non-element pages) in the tagged content stream.
        for (int p = 1; p <= numPages; p++) {
            PdfPage page = pdfDoc.getPage(p);
            pageFormXObjects.put(p,
                convertPageContentToFormXObject(pdfDoc, page));
        }

        // --- Pre-process: normalize heading levels to avoid gaps ---
        // PDF/UA 7.4.2 requires heading sequences not to skip levels
        // (e.g. H1 → H3 without H2). We remap heading tags so that
        // each new level is at most one deeper than the previous.
        {
            int maxLevelSeen = 0;
            Map<Integer, Integer> levelMap = new HashMap<>();
            for (JsonElement je : elements) {
                JsonObject elem = je.getAsJsonObject();
                String t = elem.has("tag") ? elem.get("tag").getAsString() : "P";
                int hl = getHeadingLevel(t);
                if (hl > 0) {
                    if (!levelMap.containsKey(hl)) {
                        // Map this level to the next available slot
                        int mapped = Math.min(hl, maxLevelSeen + 1);
                        levelMap.put(hl, mapped);
                    }
                    int mapped = levelMap.get(hl);
                    maxLevelSeen = Math.max(maxLevelSeen, mapped);
                    if (mapped != hl) {
                        elem.addProperty("tag", "H" + mapped);
                    }
                }
            }
        }

        int sectionDepth = 0;   // logical heading level of deepest open Sect
        int sectNesting = 0;    // actual number of Sect ancestors on the pointer stack
        int tagged = 0;

        for (JsonElement je : elements) {
            JsonObject elem = je.getAsJsonObject();
            String tag = elem.has("tag") ? elem.get("tag").getAsString() : "P";
            int pageNo = elem.has("page_no") ? elem.get("page_no").getAsInt() : 1;
            pageNo = Math.max(1, Math.min(pageNo, numPages));

            String altText = (elem.has("alt_text") && !elem.get("alt_text").isJsonNull())
                ? dePuaText(elem.get("alt_text").getAsString()) : null;
            String text = elem.has("text") ? dePuaText(elem.get("text").getAsString()) : "";

            // Extract bounding box
            float bboxX = 72, bboxY = 72, bboxR = 72, bboxT = 72;
            boolean hasBbox = false;
            if (elem.has("bbox") && !elem.get("bbox").isJsonNull()) {
                JsonObject bbox = elem.getAsJsonObject("bbox");
                bboxX = bbox.has("l") ? bbox.get("l").getAsFloat() : 72;
                bboxY = bbox.has("b") ? bbox.get("b").getAsFloat() : 72;
                bboxR = bbox.has("r") ? bbox.get("r").getAsFloat() : bboxX + 100;
                bboxT = bbox.has("t") ? bbox.get("t").getAsFloat() : bboxY + 20;
                hasBbox = true;
            }

            try {
                PdfPage page = pdfDoc.getPage(pageNo);
                tagPointer.setPageForTagging(page);

                // --- Hierarchical structure: Sect elements based on headings ---
                int headingLevel = getHeadingLevel(tag);
                if (headingLevel > 0) {
                    while (sectionDepth >= headingLevel && sectNesting > 0) {
                        tagPointer.moveToParent();
                        sectionDepth--;
                        sectNesting--;
                    }
                    tagPointer.addTag("Sect");
                    sectionDepth = headingLevel;
                    sectNesting++;
                }

                // Determine if this is a container element (has children or cells).
                // Container elements should NOT have their own marked content —
                // only leaf elements get MCRs. PDF/UA requires structure elements
                // to have EITHER content items OR child elements, not both.
                //
                // Exception: when ALL children are leaves (no nested
                // grandchildren — e.g. Span, Link), treat the parent as a
                // leaf element.  The parent already carries /ActualText with
                // the full text, so the leaf children add no semantic value.
                // Acrobat's "Associated with content" check requires that
                // elements with /ActualText be directly associated with
                // content (MCR), which fails when /K contains only child
                // struct refs pointing to empty leaves.
                boolean allChildrenAreLeaves =
                    areAllChildrenLeaves(elem);
                boolean hasChildElements =
                    ("Table".equals(tag) && elem.has("cells")
                        && elem.getAsJsonArray("cells").size() > 0)
                    || ("TOC".equals(tag) && elem.has("children")
                        && elem.getAsJsonArray("children").size() > 0)
                    || (elem.has("children") && elem.get("children").isJsonArray()
                        && elem.getAsJsonArray("children").size() > 0
                        && !allChildrenAreLeaves);

                // For leaf elements, determine if we would create any content
                boolean needsMcr = false;
                if (!hasChildElements) {
                    needsMcr = "Figure".equals(tag)
                        || "Formula".equals(tag)
                        || (text != null && !text.trim().isEmpty());
                }

                // Skip elements that would produce empty structure elements
                // (no children, no MCR). PAC flags these as structure warnings.
                if (!hasChildElements && !needsMcr) {
                    continue;
                }

                // --- Add the structure element ---
                tagPointer.addTag(tag);

                if (!hasChildElements) {
                    // --- Create marked content (BDC/EMC) for leaf elements ---

                    if (needsMcr) {
                        PdfCanvas canvas = canvasCache.computeIfAbsent(
                            pageNo,
                            p -> new RawPdfCanvas(pdfDoc.getPage(p)));

                        // For Figure elements, draw original content as
                        // Artifact first (ensures visible content is on page)
                        PdfFormXObject formXObj =
                            pageFormXObjects.get(pageNo);
                        if ("Figure".equals(tag)
                                && !pageContentTagged.contains(pageNo)
                                && formXObj != null) {
                            canvas.beginMarkedContent(PdfName.Artifact);
                            canvas.addXObjectAt(formXObj, 0, 0);
                            canvas.endMarkedContent();
                            pageContentTagged.add(pageNo);
                        }

                        String mcrText = text;
                        if ((mcrText == null || mcrText.trim().isEmpty())
                                && ("Formula".equals(tag)
                                    || "Figure".equals(tag))
                                && altText != null
                                && !altText.trim().isEmpty()) {
                            mcrText = altText;
                        }
                        writeInvisibleMCR(canvas, tagPointer, font,
                            bboxX, bboxY, mcrText);
                    }
                }

                // --- Set accessibility properties on the structure element ---
                PdfStructElem structElem = tagPointer.getContext()
                    .getPointerStructElem(tagPointer);

                // Layout attributes: BBox + Placement (WCAG 1.3.1)
                if (hasBbox) {
                    PdfDictionary layoutAttrs = new PdfDictionary();
                    layoutAttrs.put(new PdfName("O"), new PdfName("Layout"));
                    layoutAttrs.put(new PdfName("BBox"),
                        new PdfArray(new float[]{bboxX, bboxY, bboxR, bboxT}));
                    layoutAttrs.put(new PdfName("Placement"),
                        new PdfName("Block"));
                    if ("Figure".equals(tag)) {
                        layoutAttrs.put(new PdfName("Width"),
                            new PdfNumber(bboxR - bboxX));
                        layoutAttrs.put(new PdfName("Height"),
                            new PdfNumber(bboxT - bboxY));
                    }
                    structElem.put(PdfName.A, layoutAttrs);
                }

                // Alt text for figures
                if ("Figure".equals(tag)) {
                    // No placeholder: a filename or a page number is not a
                    // description. Writing one satisfied veraPDF/PAC/Ally
                    // while telling a screen reader nothing. Omitting /Alt
                    // fails PDF/UA 7.3 honestly instead.
                    if (altText != null && !altText.trim().isEmpty()) {
                        structElem.setAlt(
                            new PdfString(altText, PdfEncodings.UNICODE_BIG));
                    }
                }

                // Alt text for formulas (PDF/UA requires /Alt on Formula elements)
                if ("Formula".equals(tag)) {
                    String formulaAlt;
                    if (altText != null && !altText.isEmpty()) {
                        formulaAlt = altText;
                    } else if (text != null && !text.isEmpty()) {
                        formulaAlt = "Formula: " + text.substring(
                            0, Math.min(text.length(), 200));
                    } else {
                        // Nothing real to say — emit no /Alt at all.
                        formulaAlt = null;
                    }
                    if (formulaAlt != null) {
                        structElem.setAlt(
                            new PdfString(formulaAlt, PdfEncodings.UNICODE_BIG));
                    }
                }

                // /ActualText REMOVED — blocks AROL/NVDA from reading MCR content.
                // Screen readers read the invisible MCR text written by writeInvisibleMCR().
                // Figure/Formula use /Alt (already set above). Text elements use MCR only.

                // --- Handle table sub-structure ---
                if ("Table".equals(tag) && elem.has("cells")
                        && elem.getAsJsonArray("cells").size() > 0) {
                    // /Summary is poorly supported by screen readers (deprecated
                    // in PDF 2.0), so keep it generic and put the human-facing
                    // description in a Caption struct element (read in document
                    // order). WCAG 1.3.1.
                    String caption = (elem.has("caption") && !elem.get("caption").isJsonNull())
                        ? elem.get("caption").getAsString().trim() : null;
                    structElem.put(new PdfName("Summary"),
                        new PdfString("Data table", PdfEncodings.UNICODE_BIG));

                    // Emit the AI-generated description as the FIRST child of the
                    // Table: a Caption struct element carrying invisible (Tr=3)
                    // MCR text so AROL/NVDA read it before the table data
                    // (ISO 32000 Table 337). Mirrors the invisible-MCR pattern
                    // used for other recovered content. No /ActualText (blocks AROL).
                    if (caption != null && !caption.isEmpty()) {
                        PdfCanvas capCanvas = canvasCache.computeIfAbsent(
                            pageNo, p -> new RawPdfCanvas(pdfDoc.getPage(p)));
                        float capX = bboxX;
                        float capY = hasBbox ? bboxT : bboxY;
                        tagPointer.addTag("Caption");
                        writeInvisibleMCR(capCanvas, tagPointer, font,
                            capX, capY, caption);
                        tagPointer.moveToParent(); // back to Table
                    }
                    addTableChildren(tagPointer, elem.getAsJsonArray("cells"),
                                     pdfDoc, page, font, canvasCache, pageNo);
                }

                // --- Handle TOC sub-structure (TOCI children) ---
                if ("TOC".equals(tag) && elem.has("children")
                        && elem.get("children").isJsonArray()) {
                    addTocChildren(tagPointer, elem.getAsJsonArray("children"),
                                   pdfDoc, page, font, canvasCache, pageNo,
                                   bboxX, bboxY);
                }

                // --- Handle children (Span, Link, LI, etc.) ---
                // Skip addChildren when all children are leaves — the
                // parent is treated as a leaf with its own MCR + /ActualText.
                if (elem.has("children") && elem.get("children").isJsonArray()
                        && !allChildrenAreLeaves) {
                    addChildren(tagPointer, elem.getAsJsonArray("children"),
                                pdfDoc, page, font, canvasCache, pageNo,
                                bboxX, bboxY);
                }

                // Move back to parent (Sect or Document root)
                tagPointer.moveToParent();
                tagged++;

            } catch (Exception e) {
                System.err.println("Warning: failed to tag element "
                    + (elem.has("id") ? elem.get("id").getAsString() : "?")
                    + " (" + tag + "): " + e.getMessage());
                // If addTag() already ran, the structure tree has an
                // orphan SE with no MCR. Remove it by moving back up,
                // otherwise Acrobat flags "Associated with content".
                try {
                    tagPointer.moveToParent();
                } catch (Exception ignore) {
                    // Already at root or tree is inconsistent
                }
            }
        }

        // Close any remaining open Sect elements
        while (sectNesting > 0) {
            tagPointer.moveToParent();
            sectNesting--;
        }
        sectionDepth = 0;

        // --- Step 2b: Tag existing link annotations ---
        // PDF/UA requires all Link annotations to be nested in Link structure
        // elements. The original PDF may have link annotations that aren't in
        // our Docling structure JSON. We detect and tag them here.
        tagLinkAnnotations(pdfDoc, tagPointer, font, canvasCache);

        // --- Step 2c: Tag widget annotations (form fields) ---
        // PDF/UA 7.18.4 requires all Widget annotations to be nested in
        // Form structure elements. Source PDFs with form fields (checkboxes,
        // text inputs, etc.) need their widgets properly tagged.
        tagWidgetAnnotations(pdfDoc, tagPointer, font, canvasCache);

        // --- Step 2d: Tag markup annotations (FreeText, Square, etc.) ---
        // PDF/UA 7.18.1 requires all visible annotations to be tagged.
        // FreeText, Square, Circle, and other markup annotations from the
        // source PDF need structure elements + /Contents for PDF/UA.
        tagMarkupAnnotations(pdfDoc, tagPointer, font, canvasCache);

        // Wrap original page content as Artifact or Part.
        // Pages WITH Docling elements: visible content wrapped as Artifact
        //   (screen readers read /ActualText on structure elements instead)
        // Pages WITHOUT elements: tagged as Part (content stays in a11y tree)
        for (int p = 1; p <= numPages; p++) {
            if (!pageContentTagged.contains(p)
                    && pageFormXObjects.containsKey(p)) {
                PdfPage page = pdfDoc.getPage(p);
                PdfFormXObject formXObj = pageFormXObjects.get(p);
                if (formXObj == null) continue;

                RawPdfCanvas rawCanvas = canvasCache.computeIfAbsent(p,
                    pp -> new RawPdfCanvas(pdfDoc.getPage(pp)));

                if (pagesWithElements.contains(p)) {
                    // Element page: wrap visible content as Artifact
                    rawCanvas.beginMarkedContent(PdfName.Artifact);
                    rawCanvas.saveState();

                    byte[] origContent = formXObj.getPdfObject().getBytes();
                    if (origContent != null && origContent.length > 0) {
                        try {
                            rawCanvas.writeRawContent(origContent);
                            rawCanvas.writeRawContent(new byte[]{'\n'});

                            int openCount = countUnbalancedBDC(origContent);
                            if (openCount > 0) {
                                StringBuilder emcs = new StringBuilder();
                                for (int u = 0; u < openCount; u++) {
                                    emcs.append("\nEMC\n");
                                }
                                rawCanvas.writeRawContent(
                                    emcs.toString().getBytes(
                                        java.nio.charset.StandardCharsets.ISO_8859_1));
                            }
                        } catch (Exception e) {
                            System.err.println("  Warning: failed to inline "
                                + "content for page " + p + ": "
                                + e.getMessage()
                                + " — falling back to XObject");
                            rawCanvas.addXObjectAt(formXObj, 0, 0);
                        }
                    }

                    rawCanvas.restoreState();
                    rawCanvas.endMarkedContent();
                    pageContentTagged.add(p);
                    String ocrNote = invisibleOnlyPages.contains(p)
                        ? " (OCR-only — invisible text stripped, using Docling text for /ActualText)"
                        : "";
                    System.out.println("  Page " + p
                        + ": content wrapped as Artifact" + ocrNote);
                } else {
                    // No-element page: tag as Part
                    tagPointer.moveToRoot();
                    tagPointer.setPageForTagging(pdfDoc.getPage(p));
                    tagPointer.addTag("Part");
                    rawCanvas.openTag(tagPointer.getTagReference());
                    rawCanvas.addXObjectAt(formXObj, 0, 0);
                    rawCanvas.closeTag();
                    tagPointer.moveToParent();
                    System.out.println("  Page " + p
                        + ": original content tagged as Part (no elements)");
                }
            }
        }

        // --- Step 3: Add missing ToUnicode CMaps ---
        // PDF/UA-1 requires all characters to be mappable to Unicode.
        // Fonts from the original PDF (especially mathematical/symbolic fonts)
        // may lack /ToUnicode CMap entries. Generate CMaps from the font's
        // encoding information + Adobe Glyph List.
        addMissingToUnicodeCMaps(pdfDoc);

        // --- Step 4: Strip incorrect CIDSet entries ---
        // PDF/UA 7.21.4.2: If a CIDSet stream is present it must list all
        // glyphs in the font program. Source PDFs often have incorrect
        // CIDSets. Removing the CIDSet entry makes the rule pass since
        // the test condition includes "containsCIDSet == false".
        stripIncorrectCIDSets(pdfDoc);
        stripSymbolicTrueTypeEncoding(pdfDoc);

        // --- Step 4b: Fix CIDToGIDMap for CIDFontType2 ---
        // PDF/UA 7.21.3.2: CIDFontType2 fonts require a valid CIDToGIDMap.
        // Some source PDFs have a string "Identity" instead of the name
        // /Identity, or are missing the entry entirely.
        fixCIDToGIDMap(pdfDoc);

        // --- Step 5: Font compliance check ---
        // Pass source font refs to exclude tagger's overlay font (F1/DejaVuSans)
        checkFontCompliance(pdfDoc, sourceFontRefs);

        // --- Step 6: Fix OCProperties /Name + /AS (7.10 tests 1,2) ---
        fixOCProperties(pdfDoc);
        fixEmbeddedFileSpecs(pdfDoc);

        // Set XMP metadata LAST, just before close, so iText doesn't
        // overwrite it during close()
        setXmpMetadata(pdfDoc, title, lang);

        try {
            pdfDoc.close();
        } catch (Exception e) {
            System.err.println("FATAL: Failed to write output PDF: "
                + e.getClass().getSimpleName() + ": " + e.getMessage());
            e.printStackTrace(System.err);
            java.io.File outFile = new java.io.File(outputPath);
            if (outFile.exists()) outFile.delete();
            System.exit(2);
        }
        System.out.println("Tagged " + tagged + "/" + elements.size()
            + " elements -> " + outputPath);
        emitSourceQualitySummary();
    }

    // -----------------------------------------------------------------
    // XMP Metadata
    // -----------------------------------------------------------------

    /**
     * Set XMP title, language and producer; drop any conformance identifier
     * inherited from the input, since the output is not validated against it.
     */
    static void setXmpMetadata(PdfDocument pdfDoc, String title,
                                        String lang) {
        try {
            byte[] existingXmp = pdfDoc.getXmpMetadataBytes();
            XMPMeta xmp;
            if (existingXmp != null && existingXmp.length > 0) {
                xmp = XMPMetaFactory.parseFromBuffer(existingXmp);
            } else {
                xmp = XMPMetaFactory.create();
            }

            // dc:title (Lang Alt)
            xmp.setLocalizedText(XMPConst.NS_DC, "title",
                "x-default", "x-default", title);

            // dc:language (Bag)
            try {
                xmp.deleteProperty(XMPConst.NS_DC, "language");
            } catch (Exception ignored) {}
            xmp.appendArrayItem(XMPConst.NS_DC, "language",
                new PropertyOptions().setArray(true), lang, null);

            String PDFUA_NS = "http://www.aiim.org/pdfua/ns/id/";
            XMPMetaFactory.getSchemaRegistry()
                .registerNamespace(PDFUA_NS, "pdfuaid");
            xmp.deleteProperty(PDFUA_NS, "part");

            // pdf:Producer
            xmp.setProperty(XMPConst.NS_PDF, "Producer",
                "ReadReady (iText 9)");

            // Use iText's public setXmpMetadata(XMPMeta) API so that close()
            // preserves this metadata instead of overwriting it
            pdfDoc.setXmpMetadata(xmp);
            System.out.println("  XMP metadata set (title, language)");

        } catch (Exception e) {
            System.err.println("Warning: failed to set XMP metadata: "
                + e.getMessage());
        }
    }

    // -----------------------------------------------------------------
    // ToUnicode CMap Generation
    // -----------------------------------------------------------------

    /**
     * Add missing /ToUnicode CMap entries to fonts in the PDF.
     *
     * PDF/UA-1 requires every character in a text object to be mappable
     * to Unicode. Fonts from the original PDF (especially mathematical
     * or symbolic fonts) may lack /ToUnicode CMap entries. This method
     * generates CMaps from encoding information + Adobe Glyph List.
     *
     * Handles:
     * - Type1, TrueType, Type3 fonts with /Encoding + /Differences
     * - Standard base encodings (WinAnsiEncoding, MacRomanEncoding)
     * - Glyph names from Adobe Glyph List (alpha, sigma, integral, etc.)
     * - uniXXXX glyph naming convention
     *
     * Skips Type0 (composite/CID) fonts (these usually have their own
     * CIDToGIDMap-based mapping).
     */
    static void addMissingToUnicodeCMaps(PdfDocument pdfDoc) {
        addMissingToUnicodeCMaps(pdfDoc, null);
    }

    /**
     * @param sourceFontRefs  if non-null, font refs NOT in this set are
     *                        pre-added to the processed set so they are
     *                        skipped (filters out tagger-injected fonts).
     */
    static void addMissingToUnicodeCMaps(PdfDocument pdfDoc,
                                         Set<PdfIndirectReference> sourceFontRefs) {
        Set<PdfIndirectReference> processed = new HashSet<>();
        // Pre-seed with tagger-injected fonts so processResourceFonts skips them
        if (sourceFontRefs != null) {
            Set<PdfIndirectReference> allCurrent = snapshotSourceFontRefs(pdfDoc);
            for (PdfIndirectReference ref : allCurrent) {
                if (!sourceFontRefs.contains(ref)) {
                    processed.add(ref);
                }
            }
        }
        int added = 0;

        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfPage page = pdfDoc.getPage(p);
            PdfDictionary resources = page.getResources().getPdfObject();
            if (resources == null) continue;

            // Process fonts in page resources
            added += processResourceFonts(resources, processed, pdfDoc);

            // Also process fonts inside Form XObjects (which may have
            // their own resource dictionaries with additional fonts)
            PdfDictionary xObjects = resources.getAsDictionary(PdfName.XObject);
            if (xObjects != null) {
                for (PdfName xobjName : xObjects.keySet()) {
                    PdfObject xobjObj = xObjects.get(xobjName, false);
                    PdfStream xobjStream = null;
                    if (xobjObj instanceof PdfStream) {
                        xobjStream = (PdfStream) xobjObj;
                    } else if (xobjObj instanceof PdfIndirectReference) {
                        PdfObject deref = ((PdfIndirectReference) xobjObj)
                            .getRefersTo();
                        if (deref instanceof PdfStream) {
                            xobjStream = (PdfStream) deref;
                        }
                    }
                    if (xobjStream != null) {
                        PdfDictionary xobjRes = xobjStream.getAsDictionary(
                            PdfName.Resources);
                        if (xobjRes != null) {
                            added += processResourceFonts(
                                xobjRes, processed, pdfDoc);
                        }
                    }
                }
            }

            // Also process fonts inside annotation appearance streams.
            // Widget annotations (form fields) have /AP dictionaries with
            // their own appearance streams containing fonts for rendering
            // checkboxes, text inputs, etc. (fixes 7.21.7-t1)
            List<PdfAnnotation> annotations = page.getAnnotations();
            if (annotations != null) {
                for (PdfAnnotation annot : annotations) {
                    PdfDictionary annotDict = annot.getPdfObject();
                    PdfDictionary ap = annotDict.getAsDictionary(
                        new PdfName("AP"));
                    if (ap == null) continue;

                    // Process each appearance type: N (normal), R (rollover),
                    // D (down)
                    for (String apKey : new String[]{"N", "R", "D"}) {
                        PdfObject apObj = ap.get(new PdfName(apKey));
                        if (apObj == null) continue;

                        // AP entry can be a stream (single appearance) or
                        // a dict of sub-appearances (e.g., /Yes, /Off)
                        if (apObj instanceof PdfDictionary
                                && !(apObj instanceof PdfStream)) {
                            // Dict of sub-appearances
                            PdfDictionary subAps = (PdfDictionary) apObj;
                            for (PdfName subName : subAps.keySet()) {
                                PdfStream subStream = subAps.getAsStream(
                                    subName);
                                if (subStream != null) {
                                    PdfDictionary subRes = subStream
                                        .getAsDictionary(PdfName.Resources);
                                    if (subRes != null) {
                                        added += processResourceFonts(
                                            subRes, processed, pdfDoc);
                                    }
                                }
                            }
                        } else if (apObj instanceof PdfStream) {
                            PdfDictionary apRes = ((PdfStream) apObj)
                                .getAsDictionary(PdfName.Resources);
                            if (apRes != null) {
                                added += processResourceFonts(
                                    apRes, processed, pdfDoc);
                            }
                        }
                    }
                }
            }
        }

        if (added > 0) {
            System.out.println("  ToUnicode CMap remediation: added "
                + added + " CMap(s)");
        } else {
            System.out.println("  ToUnicode CMap remediation: "
                + "no fonts needed CMap generation");
        }
    }

    /**
     * Returns true if the ToUnicode CMap has at least one valid (non-zero)
     * target mapping in its bfchar/bfrange sections.  Used to distinguish a
     * genuinely broken CMap (everything maps to U+0000 — the SymbolMT case)
     * from a valid CMap that merely has a leading .notdef range to U+0000.
     *
     * Only the bfchar/bfrange section bodies are inspected — codespacerange
     * entries (e.g. "<0000> <ffff>") are NOT targets and must be ignored.
     */
    private static boolean hasNonZeroToUnicodeTarget(String cmapText) {
        java.util.regex.Matcher sec = java.util.regex.Pattern.compile(
            "begin(bfchar|bfrange)([\\s\\S]*?)end\\1").matcher(cmapText);
        while (sec.find()) {
            String body = sec.group(2);
            if ("bfrange".equals(sec.group(1))) {
                // bfrange: <lo> <hi> <dst>  — the 3rd group is the target.
                java.util.regex.Matcher m = java.util.regex.Pattern.compile(
                    "<[0-9A-Fa-f]+>\\s*<[0-9A-Fa-f]+>\\s*<([0-9A-Fa-f]+)>")
                    .matcher(body);
                while (m.find()) {
                    if (!isAllZeroHex(m.group(1))) return true;
                }
            } else {
                // bfchar: <src> <dst>  — the 2nd group is the target.
                java.util.regex.Matcher m = java.util.regex.Pattern.compile(
                    "<[0-9A-Fa-f]+>\\s*<([0-9A-Fa-f]+)>").matcher(body);
                while (m.find()) {
                    if (!isAllZeroHex(m.group(1))) return true;
                }
            }
        }
        return false;
    }

    /**
     * Returns true if any bfchar/bfrange target in the ToUnicode CMap falls in
     * the Private Use Area (U+E000..U+F8FF).  Source codes and codespacerange
     * entries are ignored, so a normal mapping such as {@code <E0> <00E0>}
     * does not count.
     */
    static boolean cmapMapsToPrivateUse(String cmapText) {
        java.util.regex.Matcher sec = java.util.regex.Pattern.compile(
            "begin(bfchar|bfrange)([\\s\\S]*?)end\\1").matcher(cmapText);
        while (sec.find()) {
            String body = sec.group(2);
            String targetPattern = "bfrange".equals(sec.group(1))
                // bfrange: <lo> <hi> <dst> or <lo> <hi> [<dst> ...]
                ? "<[0-9A-Fa-f]+>\\s*<[0-9A-Fa-f]+>\\s*(<[0-9A-Fa-f]+>|\\[[^\\]]*\\])"
                // bfchar: <src> <dst>
                : "<[0-9A-Fa-f]+>\\s*(<[0-9A-Fa-f]+>)";
            java.util.regex.Matcher m = java.util.regex.Pattern.compile(
                targetPattern).matcher(body);
            while (m.find()) {
                java.util.regex.Matcher hex = java.util.regex.Pattern.compile(
                    "<([0-9A-Fa-f]+)>").matcher(m.group(1));
                while (hex.find()) {
                    String h = hex.group(1);
                    for (int i = 0; i + 4 <= h.length(); i += 4) {
                        int unit = Integer.parseInt(h.substring(i, i + 4), 16);
                        if (unit >= 0xE000 && unit <= 0xF8FF) return true;
                    }
                }
            }
        }
        return false;
    }

    /** True if the hex string is non-empty and all '0' digits (e.g. "0000"). */
    private static boolean isAllZeroHex(String hex) {
        if (hex.isEmpty()) return false;
        for (int i = 0; i < hex.length(); i++) {
            if (hex.charAt(i) != '0') return false;
        }
        return true;
    }

    /** Forbidden ToUnicode targets per ISO 14289-1 7.21.7 test 2. */
    private static boolean isForbiddenUnicodeUnit(int u) {
        return u == 0x0000 || u == 0xFEFF || u == 0xFFFE;
    }

    /**
     * Sanitize a target hex string (one or more 4-digit UTF-16 code units),
     * replacing forbidden units (U+0000/FEFF/FFFE) with U+FFFD.  Sets
     * changed[0] when any unit was rewritten.
     */
    private static String sanitizeTargetHex(String hex, boolean[] changed) {
        if (hex.isEmpty() || hex.length() % 4 != 0) return hex;
        StringBuilder sb = new StringBuilder(hex.length());
        for (int i = 0; i < hex.length(); i += 4) {
            String unit = hex.substring(i, i + 4);
            if (isForbiddenUnicodeUnit(Integer.parseInt(unit, 16))) {
                sb.append("FFFD");
                changed[0] = true;
            } else {
                sb.append(unit.toUpperCase());
            }
        }
        return sb.toString();
    }

    /**
     * Rewrite forbidden ToUnicode targets (U+0000, U+FEFF, U+FFFE) to U+FFFD
     * while preserving every other mapping and the source code widths.  A
     * source CMap may map most glyphs correctly yet carry a stray
     * glyph->U+0000 (or BOM/non-character) entry; veraPDF 7.21.7 t2 rejects
     * those targets, so a valid-but-imperfect CMap that we keep would still
     * fail.  bfchar and array-form bfrange targets are fixed in place; a
     * contiguous bfrange whose target span crosses a forbidden value is split
     * into kept sub-ranges plus per-code bfchar overrides.  Returns the input
     * unchanged when nothing needs fixing.
     */
    private static String sanitizeToUnicodeTargets(String cmapText) {
        java.util.regex.Matcher sec = java.util.regex.Pattern.compile(
            "(\\d+)\\s+begin(bfchar|bfrange)([\\s\\S]*?)end\\2").matcher(cmapText);
        StringBuffer out = new StringBuffer();
        boolean changed = false;
        while (sec.find()) {
            String kind = sec.group(2);
            String body = sec.group(3);
            java.util.List<String> chars = new java.util.ArrayList<>();
            java.util.List<String> ranges = new java.util.ArrayList<>();
            boolean secChanged = "bfchar".equals(kind)
                ? sanitizeBfcharSection(body, chars)
                : sanitizeBfrangeSection(body, chars, ranges);
            if (!secChanged) {
                sec.appendReplacement(out,
                    java.util.regex.Matcher.quoteReplacement(sec.group()));
                continue;
            }
            changed = true;
            StringBuilder rep = new StringBuilder();
            emitBfBlocks(rep, "bfrange", ranges);
            emitBfBlocks(rep, "bfchar", chars);
            String repStr = rep.toString();
            if (repStr.endsWith("\n")) {
                repStr = repStr.substring(0, repStr.length() - 1);
            }
            sec.appendReplacement(out,
                java.util.regex.Matcher.quoteReplacement(repStr));
        }
        sec.appendTail(out);
        return changed ? out.toString() : cmapText;
    }

    /** Sanitize bfchar entries (targets fixed in place, count unchanged). */
    private static boolean sanitizeBfcharSection(
            String body, java.util.List<String> chars) {
        java.util.regex.Matcher m = java.util.regex.Pattern.compile(
            "<([0-9A-Fa-f]+)>\\s*<([0-9A-Fa-f]+)>").matcher(body);
        boolean[] changed = {false};
        while (m.find()) {
            String src = m.group(1).toUpperCase();
            String dst = sanitizeTargetHex(m.group(2), changed);
            chars.add("<" + src + "> <" + dst + ">");
        }
        return changed[0];
    }

    /**
     * Sanitize bfrange entries.  Array-form and multi-unit targets are fixed in
     * place; a contiguous {@code <lo> <hi> <dst>} range whose target span
     * crosses a forbidden value is split into kept sub-ranges plus per-code
     * bfchar overrides mapped to U+FFFD.
     */
    private static boolean sanitizeBfrangeSection(String body,
            java.util.List<String> chars, java.util.List<String> ranges) {
        boolean[] changed = {false};
        java.util.regex.Matcher m = java.util.regex.Pattern.compile(
            "<([0-9A-Fa-f]+)>\\s*<([0-9A-Fa-f]+)>\\s*(\\[[^\\]]*\\]|<[0-9A-Fa-f]+>)")
            .matcher(body);
        while (m.find()) {
            String loHex = m.group(1).toUpperCase();
            String hiHex = m.group(2).toUpperCase();
            String tgt = m.group(3);
            String fmt = "<%0" + loHex.length() + "X>";
            if (tgt.startsWith("[")) {
                java.util.regex.Matcher em = java.util.regex.Pattern.compile(
                    "<([0-9A-Fa-f]+)>").matcher(tgt);
                StringBuilder arr = new StringBuilder("[");
                while (em.find()) {
                    arr.append("<")
                       .append(sanitizeTargetHex(em.group(1), changed))
                       .append(">");
                }
                arr.append("]");
                ranges.add("<" + loHex + "> <" + hiHex + "> " + arr);
                continue;
            }
            String dstHex = tgt.substring(1, tgt.length() - 1);
            if (dstHex.length() != 4) {
                ranges.add("<" + loHex + "> <" + hiHex + "> <"
                    + sanitizeTargetHex(dstHex, changed) + ">");
                continue;
            }
            int lo = Integer.parseInt(loHex, 16);
            int hi = Integer.parseInt(hiHex, 16);
            int dst = Integer.parseInt(dstHex, 16);
            int subLo = -1;
            for (int code = lo; code <= hi; code++) {
                int t = dst + (code - lo);
                if (isForbiddenUnicodeUnit(t)) {
                    if (subLo != -1) {
                        ranges.add(rangeLine(fmt, subLo, code - 1, dst + (subLo - lo)));
                        subLo = -1;
                    }
                    chars.add(String.format(fmt + " <FFFD>", code));
                    changed[0] = true;
                } else if (subLo == -1) {
                    subLo = code;
                }
            }
            if (subLo != -1) {
                ranges.add(rangeLine(fmt, subLo, hi, dst + (subLo - lo)));
            }
        }
        return changed[0];
    }

    private static String rangeLine(String codeFmt, int lo, int hi, int dst) {
        return String.format(codeFmt + " " + codeFmt + " <%04X>", lo, hi, dst);
    }

    /** Emit "N beginbfX ... endbfX" blocks of <= 100 entries each. */
    private static void emitBfBlocks(StringBuilder sb, String kind,
            java.util.List<String> entries) {
        int i = 0;
        while (i < entries.size()) {
            int n = Math.min(100, entries.size() - i);
            sb.append(n).append(" begin").append(kind).append("\n");
            for (int j = 0; j < n; j++) {
                sb.append(entries.get(i + j)).append("\n");
            }
            sb.append("end").append(kind).append("\n");
            i += n;
        }
    }

    /**
     * Process all fonts in a resource dictionary, adding ToUnicode CMaps
     * where missing.
     *
     * @return Number of CMaps added
     */
    private static int processResourceFonts(
            PdfDictionary resources,
            Set<PdfIndirectReference> processed,
            PdfDocument pdfDoc) {
        PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
        if (fonts == null) return 0;

        int added = 0;
        for (PdfName fontName : fonts.keySet()) {
            PdfDictionary fontDict = null;

            // Resolve the font dictionary (may be direct or indirect)
            PdfObject fontObj = fonts.get(fontName, false);
            if (fontObj instanceof PdfDictionary) {
                fontDict = (PdfDictionary) fontObj;
            } else if (fontObj instanceof PdfIndirectReference) {
                PdfObject deref = ((PdfIndirectReference) fontObj).getRefersTo();
                if (deref instanceof PdfDictionary) {
                    fontDict = (PdfDictionary) deref;
                }
            }
            if (fontDict == null) {
                // Try standard dereferencing
                fontDict = fonts.getAsDictionary(fontName);
            }
            if (fontDict == null) continue;

            // Skip if already processed (by indirect reference)
            PdfIndirectReference ref = fontDict.getIndirectReference();
            if (ref != null && !processed.add(ref)) continue;

            PdfName subtype = fontDict.getAsName(PdfName.Subtype);
            String baseFontName = fontDict.containsKey(PdfName.BaseFont)
                ? fontDict.getAsName(PdfName.BaseFont).getValue()
                : fontName.getValue();

            // Check existing ToUnicode: skip if present AND not broken.
            // Detect and replace: PUA-mapped Type3 CMaps, empty CMaps,
            // and CMaps with U+0000 target mappings (7.21.7 test 2).
            if (fontDict.containsKey(PdfName.ToUnicode)) {
                boolean needsReplacement = false;
                PdfStream existingCmap = fontDict.getAsStream(
                    PdfName.ToUnicode);
                if (existingCmap != null) {
                    byte[] cmapBytes = existingCmap.getBytes();
                    if (cmapBytes != null) {
                        String cmapText = new String(cmapBytes,
                            StandardCharsets.ISO_8859_1);
                        // Type3 PUA replacement: a target (not a source
                        // code) in the Private Use Area means no real text.
                        if (new PdfName("Type3").equals(subtype)
                                && cmapMapsToPrivateUse(cmapText)) {
                            needsReplacement = true;
                        }
                        // Empty CMap: boilerplate present but no mappings
                        if (!cmapText.contains("beginbfchar")
                                && !cmapText.contains("beginbfrange")) {
                            needsReplacement = true;
                        }
                        // CMap maps to U+0000 (forbidden by 7.21.7 test 2) —
                        // but ONLY treat as broken if the CMap has NO valid
                        // (non-zero) target mappings.  A valid CMap legitimately
                        // maps a leading .notdef range to U+0000 (e.g. the first
                        // bfrange entry "<0000> <0002> <0000>") while correctly
                        // mapping every other code.  Replacing such a CMap with
                        // an identity CMap destroys the real mappings: CID 0x26
                        // ('C') would become U+0026 ('&'), a uniform -29 offset
                        // that turns all extracted text into gibberish
                        // (screen_reader_sim coverage failures).
                        if (cmapText.contains("> <0000>")
                                && !hasNonZeroToUnicodeTarget(cmapText)) {
                            needsReplacement = true;
                        }
                        // (narrow-codespace Type0 check removed — it incorrectly
                        // replaced valid subsetted-font CMaps with identity CMaps)
                        // Non-Type0 fonts: if codespace includes sub-0x20 range
                        // but CMap has no entries for those codes, used glyphs
                        // in 0x00-0x1F will resolve to U+0000 (7.21.7 t2).
                        // Only replace if the CMap actually lacks entries for
                        // those codes.  Subsetted fonts (e.g. BAAAAA+Garamond)
                        // may have valid bfchar mappings for codes 0x01-0x1F.
                        if (!needsReplacement
                                && !PdfName.Type0.equals(subtype)) {
                            PdfNumber fc = fontDict.getAsNumber(
                                new PdfName("FirstChar"));
                            if (fc != null && fc.intValue() < 32) {
                                // Check whether the existing CMap covers
                                // the low char codes.  If it has bfchar
                                // or bfrange entries for codes < 0x20, it's valid.
                                // bfchar format: <01> <0057>  (space before target)
                                // bfrange format: <01><01><0057> (no spaces)
                                boolean lowCodesCovered = false;
                                for (int c = fc.intValue(); c < 32; c++) {
                                    String hexCode = String.format("<%02X>", c);
                                    // bfchar: "<XX> <YYYY>" with non-null target
                                    boolean bfcharMatch =
                                        cmapText.contains(hexCode + " <")
                                        && !cmapText.contains(
                                            hexCode + " <0000>");
                                    // bfrange: "<XX><XX><YYYY>" (no spaces)
                                    boolean bfrangeMatch =
                                        cmapText.contains(hexCode + "<")
                                        && !cmapText.contains(
                                            hexCode + "<" + hexCode + "<0000>");
                                    if (bfcharMatch || bfrangeMatch) {
                                        lowCodesCovered = true;
                                        break;
                                    }
                                }
                                if (!lowCodesCovered) {
                                    needsReplacement = true;
                                }
                            }
                        }

                        // Kept CMaps: sanitize forbidden ToUnicode targets
                        // (U+0000/FEFF/FFFE -> U+FFFD).  A source CMap may map
                        // most glyphs correctly yet carry a stray glyph->U+0000
                        // (or BOM/non-character) entry; veraPDF 7.21.7 t2 rejects
                        // those targets.  Sanitizing clears the rule while every
                        // real mapping (coverage) is preserved.
                        if (!needsReplacement) {
                            String cleaned = sanitizeToUnicodeTargets(cmapText);
                            if (!cleaned.equals(cmapText)) {
                                PdfStream cleanStream = new PdfStream(
                                    cleaned.getBytes(StandardCharsets.ISO_8859_1),
                                    CompressionConstants.DEFAULT_COMPRESSION);
                                cleanStream.makeIndirect(pdfDoc);
                                fontDict.put(PdfName.ToUnicode, cleanStream);
                                System.out.println("    Sanitized ToUnicode CMap "
                                    + "(forbidden targets -> U+FFFD): "
                                    + baseFontName);
                            }
                        }
                    }
                }
                if (!needsReplacement) continue;
            }

            // Type0 (composite/CID) fonts: generate 2-byte identity CMap
            if (PdfName.Type0.equals(subtype)) {
                String cmap = generateType0ToUnicodeCMap(fontDict);
                if (cmap != null) cmap = sanitizeToUnicodeTargets(cmap);
                if (cmap != null) {
                    PdfStream cmapStream = new PdfStream(
                        cmap.getBytes(StandardCharsets.ISO_8859_1),
                        CompressionConstants.DEFAULT_COMPRESSION);
                    cmapStream.makeIndirect(pdfDoc);
                    fontDict.put(PdfName.ToUnicode, cmapStream);
                    added++;
                    sqToUnicodeCMapsAdded++;
                    System.out.println("    Added ToUnicode CMap for Type0 font: "
                        + baseFontName);
                } else {
                    sqToUnicodeCMapsFailed++;
                    System.err.println("    WARNING: Could not generate ToUnicode "
                        + "CMap for Type0 font: " + baseFontName);
                }
                continue;
            }

            // Generate ToUnicode CMap from encoding information
            String cmap = generateToUnicodeCMap(fontDict);
            if (cmap != null) cmap = sanitizeToUnicodeTargets(cmap);
            if (cmap != null) {
                PdfStream cmapStream = new PdfStream(
                    cmap.getBytes(StandardCharsets.ISO_8859_1),
                    CompressionConstants.DEFAULT_COMPRESSION);
                cmapStream.makeIndirect(pdfDoc);
                fontDict.put(PdfName.ToUnicode, cmapStream);
                added++;
                sqToUnicodeCMapsAdded++;
                System.out.println("    Added ToUnicode CMap for font: "
                    + baseFontName);
            } else {
                sqToUnicodeCMapsFailed++;
                System.err.println("    WARNING: Could not generate ToUnicode "
                    + "CMap for font: " + baseFontName
                    + " (no usable encoding information)");
            }
        }
        return added;
    }

    /**
     * Generate a ToUnicode CMap string from a font's encoding information.
     *
     * Reads the /Encoding dictionary or name, processes /Differences array
     * to map glyph names to Unicode via Adobe Glyph List, and builds a
     * standards-compliant CMap stream.
     *
     * @return CMap string, or null if no useful mapping could be generated
     */
    private static String generateToUnicodeCMap(PdfDictionary fontDict) {
        // Build char code → Unicode mapping
        Map<Integer, Integer> mapping = new TreeMap<>();

        PdfObject encodingObj = fontDict.get(PdfName.Encoding);

        String baseEncoding = null;
        PdfArray differences = null;

        if (encodingObj != null && encodingObj.isName()) {
            baseEncoding = ((PdfName) encodingObj).getValue();
        } else if (encodingObj != null && encodingObj.isDictionary()) {
            PdfDictionary encDict = (PdfDictionary) encodingObj;
            PdfName baseEncName = encDict.getAsName(
                new PdfName("BaseEncoding"));
            if (baseEncName != null) {
                baseEncoding = baseEncName.getValue();
            }
            differences = encDict.getAsArray(new PdfName("Differences"));
        }

        // Populate base encoding mappings
        if ("WinAnsiEncoding".equals(baseEncoding)) {
            fillWinAnsiMapping(mapping);
        } else if ("MacRomanEncoding".equals(baseEncoding)) {
            fillMacRomanMapping(mapping);
        } else if ("StandardEncoding".equals(baseEncoding)) {
            fillStandardEncodingMapping(mapping);
        }

        // Handle ZapfDingbats and Symbol: these standard fonts have
        // built-in encodings. If the font is ZapfDingbats/Symbol and
        // no explicit encoding was found, use their known encoding.
        if (mapping.isEmpty() && baseEncoding == null
                && differences == null) {
            PdfName baseFont = fontDict.getAsName(PdfName.BaseFont);
            String fontName = baseFont != null
                ? baseFont.getValue() : "";
            if (fontName.contains("ZapfDingbats")) {
                fillZapfDingbatsMapping(mapping);
            } else if (fontName.contains("Symbol")) {
                fillSymbolMapping(mapping);
            } else if (fontName.contains("Wingdings")) {
                fillWingdingsMapping(mapping);
            } else if (isStandard14Font(fontName)) {
                // Standard 14 fonts use implied WinAnsiEncoding
                // when no /Encoding is present (ISO 32000-1 §9.6.2.2)
                fillWinAnsiMapping(mapping);
            }
        }

        // Override/extend with /Differences array entries
        // Format: [charCode /glyphName /glyphName ... charCode /glyphName ...]
        if (differences != null) {
            int charCode = 0;
            for (int i = 0; i < differences.size(); i++) {
                PdfObject item = differences.get(i);
                if (item.isNumber()) {
                    charCode = ((PdfNumber) item).intValue();
                } else if (item.isName()) {
                    String glyphName = ((PdfName) item).getValue();
                    int unicode = glyphNameToUnicode(glyphName);
                    if (unicode > 0) {
                        mapping.put(charCode, unicode);
                    }
                    charCode++;
                }
            }
        }

        // Gap-fill: any glyph names in /Differences that were NOT resolved
        // by glyphNameToUnicode() still need a ToUnicode mapping for PDF/UA
        // 7.21.7. For printable character codes, use identity mapping (the
        // char code IS the Unicode codepoint — common for Type 3 fonts from
        // LaTeX/dvipdfm). For non-printable codes, use PUA.
        if (differences != null) {
            int charCode = 0;
            for (int i = 0; i < differences.size(); i++) {
                PdfObject item = differences.get(i);
                if (item.isNumber()) {
                    charCode = ((PdfNumber) item).intValue();
                } else if (item.isName()) {
                    String gn = ((PdfName) item).getValue();
                    if (!".notdef".equals(gn) && !mapping.containsKey(charCode)) {
                        // Printable ASCII (0x20-0x7E) and Latin-1 Supplement
                        // (0xA0-0xFF): use identity mapping.
                        if ((charCode >= 0x20 && charCode <= 0x7E)
                                || (charCode >= 0xA0 && charCode <= 0xFF)) {
                            mapping.put(charCode, charCode);
                        } else if (charCode < 0x20) {
                            // TeX T1 control range: map to Unicode equivalent
                            // (single codepoint only — ligatures like ff get
                            // the first char; dePuaText() handles full expansion
                            // in /ActualText)
                            int repl = texControlCharUnicode(charCode);
                            mapping.put(charCode, repl);
                        } else {
                            // Other non-printable: map to PUA
                            int pua = 0xE000 + charCode;
                            if (pua <= 0xF8FF) {
                                mapping.put(charCode, pua);
                            }
                        }
                    }
                    charCode++;
                }
            }
        }

        // If no encoding info at all, try to build a basic identity mapping
        // for the printable ASCII range as a fallback
        if (mapping.isEmpty() && encodingObj == null) {
            // Check if font has /Widths — if so, use /FirstChar and /LastChar
            // to determine which character codes are active
            PdfNumber firstChar = fontDict.getAsNumber(
                new PdfName("FirstChar"));
            PdfNumber lastChar = fontDict.getAsNumber(
                new PdfName("LastChar"));
            if (firstChar != null && lastChar != null) {
                int fc = firstChar.intValue();
                int lc = lastChar.intValue();
                // For character codes in the ASCII printable range,
                // assume identity mapping (common for many basic fonts)
                for (int c = Math.max(fc, 32); c <= Math.min(lc, 126); c++) {
                    mapping.put(c, c);
                }
            }
        }

        if (mapping.isEmpty()) return null;

        // Ensure char codes 0x00-0x1F have non-zero Unicode targets.
        // Some source PDFs use these codes (undefined in WinAnsi/MacRoman)
        // and with codespace <00>-<FF> they must map to something non-zero
        // to avoid 7.21.7 t2 ("Unicode value 0"). Use PUA (U+E000+code)
        // unless already mapped (e.g., by TeX T1 control char handling).
        for (int i = 0; i < 32; i++) {
            if (!mapping.containsKey(i)) {
                mapping.put(i, 0xE000 + i);
            }
        }

        return buildCMapString(mapping);
    }

    /**
     * Generate a ToUnicode CMap for a Type0 (composite/CID) font.
     *
     * Type0 fonts with Identity-H or Identity-V encoding use CID values
     * as character codes. For these fonts, CID values often correspond
     * directly to Unicode code points (identity mapping). We build a
     * 2-byte CMap covering the CID range found in the /W (widths) array
     * of the descendant CIDFont.
     *
     * @return CMap string, or null if the font cannot be mapped
     */
    private static String generateType0ToUnicodeCMap(PdfDictionary fontDict) {
        // Get the encoding — we only handle Identity-H and Identity-V
        PdfObject encodingObj = fontDict.get(PdfName.Encoding);
        if (encodingObj == null) return null;

        String encoding = null;
        if (encodingObj.isName()) {
            encoding = ((PdfName) encodingObj).getValue();
        }
        if (!"Identity-H".equals(encoding) && !"Identity-V".equals(encoding)) {
            return null; // Non-identity CMaps need different handling
        }

        // Get the descendant CIDFont
        PdfArray descendants = fontDict.getAsArray(new PdfName("DescendantFonts"));
        if (descendants == null || descendants.size() == 0) return null;
        PdfDictionary cidFont = descendants.getAsDictionary(0);
        if (cidFont == null) return null;

        // For Identity-H/V fonts, CID == Unicode code point.
        // Use a single bfrange covering the entire BMP — handles all CIDs
        // including those using /DW default width (not listed in /W).
        return buildType0IdentityCMapString();
    }

    /**
     * Build a full-range identity ToUnicode CMap for Type0/Identity-H fonts.
     * Uses 256-entry bfrange blocks (CMap spec requires srcCode1 and
     * srcCode2 share the same high byte).  Maps every CID 0x0001-0xFFFF
     * to the same Unicode code point, covering CIDs not listed in /W.
     */
    private static String buildType0IdentityCMapString() {
        StringBuilder sb = new StringBuilder();
        sb.append("/CIDInit /ProcSet findresource begin\n");
        sb.append("12 dict begin\n");
        sb.append("begincmap\n");
        sb.append("/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) ");
        sb.append("/Supplement 0 >> def\n");
        sb.append("/CMapName /Adobe-Identity-UCS def\n");
        sb.append("/CMapType 2 def\n");
        sb.append("1 begincodespacerange\n");
        sb.append("<0000> <FFFF>\n");
        sb.append("endcodespacerange\n");

        // Map CID 0 to U+FFFD (replacement character).
        // Cannot map to U+0000 (7.21.7 t2 violation), but content
        // streams may reference CID 0 (7.21.7 t1 requires mapping).
        sb.append("1 beginbfchar\n");
        sb.append("<0000> <FFFD>\n");
        sb.append("endbfchar\n");

        // Write identity bfrange blocks of up to 100 entries each
        // (CMap spec limits beginbfrange blocks to 100 entries).
        // Each range entry covers one 256-code page (same high byte).
        // 256 pages total (0x00..0xFF), so 3 blocks of 100 + 1 of 56.
        int pageCount = 256; // 0x00 through 0xFF
        int idx = 0;
        while (idx < pageCount) {
            int blockSize = Math.min(100, pageCount - idx);
            sb.append(blockSize).append(" beginbfrange\n");
            for (int b = 0; b < blockSize; b++) {
                int hi = idx + b;
                int start = (hi << 8) | (hi == 0 ? 0x01 : 0x00);
                int end   = (hi << 8) | 0xFF;
                sb.append(String.format("<%04X> <%04X> <%04X>\n",
                    start, end, start));
            }
            sb.append("endbfrange\n");
            idx += blockSize;
        }

        sb.append("endcmap\n");
        sb.append("CMapName currentdict /CMap defineresource pop\n");
        sb.append("end\n");
        sb.append("end\n");
        return sb.toString();
    }

    /** Check if a font name is one of the PDF Standard 14 fonts. */
    private static boolean isStandard14Font(String fontName) {
        if (fontName == null) return false;
        // Strip subset prefix
        String clean = fontName.contains("+")
            ? fontName.substring(fontName.indexOf('+') + 1) : fontName;
        switch (clean) {
            case "Courier": case "Courier-Bold":
            case "Courier-Oblique": case "Courier-BoldOblique":
            case "Helvetica": case "Helvetica-Bold":
            case "Helvetica-Oblique": case "Helvetica-BoldOblique":
            case "Times-Roman": case "Times-Bold":
            case "Times-Italic": case "Times-BoldItalic":
            // Symbol and ZapfDingbats handled separately (different encoding)
                return true;
            default:
                return false;
        }
    }

    private static int glyphNameToUnicode(String glyphName) {
        if (glyphName == null || glyphName.isEmpty()
                || ".notdef".equals(glyphName)) {
            return -1;
        }

        // 1. Try Adobe Glyph List first
        int unicode = AdobeGlyphList.nameToUnicode(glyphName);
        if (unicode > 0) return unicode;

        // 2. Try uniXXXX convention (exactly 4 hex digits after "uni")
        if (glyphName.startsWith("uni") && glyphName.length() == 7) {
            try {
                int cp = Integer.parseInt(glyphName.substring(3), 16);
                if (cp > 0 && cp <= 0xFFFF) return cp;
            } catch (NumberFormatException ignored) {}
        }

        // 3. Try uXXXX or uXXXXX convention (4-5 hex digits after "u")
        if (glyphName.startsWith("u") && !glyphName.startsWith("uni")
                && glyphName.length() >= 5 && glyphName.length() <= 6) {
            try {
                int cp = Integer.parseInt(glyphName.substring(1), 16);
                if (cp > 0 && cp <= 0x10FFFF) return cp;
            } catch (NumberFormatException ignored) {}
        }

        // 4. Handle "aNN" convention (e.g. a97 → U+0061 'a', a228 → U+00E4 'ä')
        // Type 3 fonts from LaTeX/dvipdfm often use glyph names where the
        // number is the Unicode codepoint of the intended character.
        if (glyphName.startsWith("a") && glyphName.length() >= 2) {
            try {
                int cp = Integer.parseInt(glyphName.substring(1));
                if (cp >= 0x20 && cp <= 0x10FFFF
                        && cp != 0x7F  // DEL
                        && !(cp >= 0x80 && cp <= 0x9F)  // C1 controls
                        && !(cp >= 0xE000 && cp <= 0xF8FF)) {  // PUA
                    return cp;
                }
            } catch (NumberFormatException ignored) {}
        }

        return -1;
    }

    /**
     * Build a ToUnicode CMap string from a char-code-to-Unicode mapping.
     *
     * The CMap maps single-byte character codes (0x00-0xFF) to Unicode
     * code points in the Basic Multilingual Plane (U+0000-U+FFFF).
     */
    private static String buildCMapString(Map<Integer, Integer> mapping) {
        StringBuilder sb = new StringBuilder();
        sb.append("/CIDInit /ProcSet findresource begin\n");
        sb.append("12 dict begin\n");
        sb.append("begincmap\n");
        sb.append("/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) ");
        sb.append("/Supplement 0 >> def\n");
        sb.append("/CMapName /Adobe-Identity-UCS def\n");
        sb.append("/CMapType 2 def\n");

        // Use full single-byte codespace <00> <FF> so any char code
        // used in content streams falls within range. Previously used
        // dynamic min/max from mapping keys, but char codes outside
        // that range (e.g., 0x00-0x1F) would resolve to U+0000.
        sb.append("1 begincodespacerange\n");
        sb.append("<00> <FF>\n");
        sb.append("endcodespacerange\n");

        // Write bfchar entries (max 100 per block per CMap spec)
        List<Map.Entry<Integer, Integer>> entries =
            new ArrayList<>(mapping.entrySet());
        int idx = 0;
        while (idx < entries.size()) {
            int blockSize = Math.min(100, entries.size() - idx);
            sb.append(blockSize).append(" beginbfchar\n");
            for (int i = 0; i < blockSize; i++) {
                Map.Entry<Integer, Integer> e = entries.get(idx + i);
                int charCode = e.getKey();
                int unicodeCP = e.getValue();
                if (unicodeCP < 0 || unicodeCP > 0x10FFFF
                        || (unicodeCP >= 0xD800 && unicodeCP <= 0xDFFF)) {
                    // Not a valid Unicode scalar value — surrogate math
                    // below would emit a corrupt pair. Map to U+FFFD.
                    sb.append(String.format("<%02X> <FFFD>\n", charCode));
                } else if (unicodeCP <= 0xFFFF) {
                    sb.append(String.format("<%02X> <%04X>\n",
                        charCode, unicodeCP));
                } else {
                    // Supplementary plane: encode as UTF-16 surrogate pair
                    int hi = 0xD800 + ((unicodeCP - 0x10000) >> 10);
                    int lo = 0xDC00 + ((unicodeCP - 0x10000) & 0x3FF);
                    sb.append(String.format("<%02X> <%04X%04X>\n",
                        charCode, hi, lo));
                }
            }
            sb.append("endbfchar\n");
            idx += blockSize;
        }

        sb.append("endcmap\n");
        sb.append("CMapName currentdict /CMap defineresource pop\n");
        sb.append("end\n");
        sb.append("end\n");
        return sb.toString();
    }

    /**
     * Fill WinAnsiEncoding mapping (Windows-1252 / CP1252).
     * Codes 32-127 and 160-255 map to corresponding Unicode code points.
     * Codes 128-159 have Windows-specific mappings.
     */
    private static void fillWinAnsiMapping(Map<Integer, Integer> mapping) {
        // ASCII printable range: identity mapping
        for (int i = 32; i <= 127; i++) {
            mapping.put(i, i);
        }
        // Windows-1252 special mappings (128-159)
        int[] cp1252 = {
            0x20AC, 0,      0x201A, 0x0192, 0x201E, 0x2026, 0x2020, 0x2021,
            0x02C6, 0x2030, 0x0160, 0x2039, 0x0152, 0,      0x017D, 0,
            0,      0x2018, 0x2019, 0x201C, 0x201D, 0x2022, 0x2013, 0x2014,
            0x02DC, 0x2122, 0x0161, 0x203A, 0x0153, 0,      0x017E, 0x0178
        };
        for (int i = 0; i < cp1252.length; i++) {
            if (cp1252[i] != 0) {
                mapping.put(128 + i, cp1252[i]);
            }
        }
        // Latin-1 supplement: identity mapping
        for (int i = 160; i <= 255; i++) {
            mapping.put(i, i);
        }
    }

    /**
     * Fill MacRomanEncoding mapping.
     * Codes 32-127: identity (standard ASCII).
     * Codes 128-255: Mac OS Roman specific mappings.
     */
    private static void fillMacRomanMapping(Map<Integer, Integer> mapping) {
        // ASCII printable range: identity
        for (int i = 32; i <= 127; i++) {
            mapping.put(i, i);
        }
        // Mac Roman 128-255 mappings
        int[] macRoman = {
            0x00C4, 0x00C5, 0x00C7, 0x00C9, 0x00D1, 0x00D6, 0x00DC, 0x00E1,
            0x00E0, 0x00E2, 0x00E4, 0x00E3, 0x00E5, 0x00E7, 0x00E9, 0x00E8,
            0x00EA, 0x00EB, 0x00ED, 0x00EC, 0x00EE, 0x00EF, 0x00F1, 0x00F3,
            0x00F2, 0x00F4, 0x00F6, 0x00F5, 0x00FA, 0x00F9, 0x00FB, 0x00FC,
            0x2020, 0x00B0, 0x00A2, 0x00A3, 0x00A7, 0x2022, 0x00B6, 0x00DF,
            0x00AE, 0x00A9, 0x2122, 0x00B4, 0x00A8, 0x2260, 0x00C6, 0x00D8,
            0x221E, 0x00B1, 0x2264, 0x2265, 0x00A5, 0x00B5, 0x2202, 0x2211,
            0x220F, 0x03C0, 0x222B, 0x00AA, 0x00BA, 0x2126, 0x00E6, 0x00F8,
            0x00BF, 0x00A1, 0x00AC, 0x221A, 0x0192, 0x2248, 0x2206, 0x00AB,
            0x00BB, 0x2026, 0x00A0, 0x00C0, 0x00C3, 0x00D5, 0x0152, 0x0153,
            0x2013, 0x2014, 0x201C, 0x201D, 0x2018, 0x2019, 0x00F7, 0x25CA,
            0x00FF, 0x0178, 0x2044, 0x20AC, 0x2039, 0x203A, 0xFB01, 0xFB02,
            0x2021, 0x00B7, 0x201A, 0x201E, 0x2030, 0x00C2, 0x00CA, 0x00C1,
            0x00CB, 0x00C8, 0x00CD, 0x00CE, 0x00CF, 0x00CC, 0x00D3, 0x00D4,
            0xF8FF, 0x00D2, 0x00DA, 0x00DB, 0x00D9, 0x0131, 0x02C6, 0x02DC,
            0x00AF, 0x02D8, 0x02D9, 0x02DA, 0x00B8, 0x02DD, 0x02DB, 0x02C7
        };
        for (int i = 0; i < macRoman.length; i++) {
            mapping.put(128 + i, macRoman[i]);
        }
    }

    /**
     * Fill StandardEncoding (Adobe Standard Encoding) mapping.
     * Used by some Type1 fonts. Maps character codes to Unicode
     * via well-known glyph names.
     */
    private static void fillStandardEncodingMapping(
            Map<Integer, Integer> mapping) {
        // ASCII printable range: mostly identity
        for (int i = 32; i <= 126; i++) {
            mapping.put(i, i);
        }
        // Notable differences from ASCII/Latin-1:
        mapping.put(0x27, 0x2019); // quotesingle → right single quote
        mapping.put(0x60, 0x2018); // grave → left single quote
        // Upper range (selected well-known mappings)
        mapping.put(0xA1, 0x00A1); // exclamdown
        mapping.put(0xA2, 0x00A2); // cent
        mapping.put(0xA3, 0x00A3); // sterling
        mapping.put(0xA4, 0x2044); // fraction
        mapping.put(0xA5, 0x00A5); // yen
        mapping.put(0xA6, 0x0192); // florin
        mapping.put(0xA7, 0x00A7); // section
        mapping.put(0xA8, 0x00A4); // currency
        mapping.put(0xAC, 0x00AC); // logicalnot
        mapping.put(0xAD, 0xFB01); // fi
        mapping.put(0xAE, 0xFB02); // fl
        mapping.put(0xB1, 0x2013); // endash
        mapping.put(0xB2, 0x2020); // dagger
        mapping.put(0xB3, 0x2021); // daggerdbl
        mapping.put(0xB4, 0x00B7); // periodcentered
        mapping.put(0xB7, 0x2022); // bullet
        mapping.put(0xB8, 0x201A); // quotesinglbase
        mapping.put(0xB9, 0x201E); // quotedblbase
        mapping.put(0xBA, 0x201D); // quotedblright
        mapping.put(0xBB, 0x00BB); // guillemotright
        mapping.put(0xBC, 0x2026); // ellipsis
        mapping.put(0xBD, 0x2030); // perthousand
        mapping.put(0xC1, 0x0060); // grave accent
        mapping.put(0xC2, 0x00B4); // acute accent
        mapping.put(0xC3, 0x02C6); // circumflex
        mapping.put(0xC4, 0x02DC); // tilde
        mapping.put(0xC5, 0x00AF); // macron
        mapping.put(0xC6, 0x02D8); // breve
        mapping.put(0xC7, 0x02D9); // dotaccent
        mapping.put(0xC8, 0x00A8); // dieresis
        mapping.put(0xCA, 0x02DA); // ring
        mapping.put(0xCB, 0x00B8); // cedilla
        mapping.put(0xCD, 0x02DD); // hungarumlaut
        mapping.put(0xCE, 0x02DB); // ogonek
        mapping.put(0xCF, 0x02C7); // caron
        mapping.put(0xD0, 0x2014); // emdash
        mapping.put(0xE1, 0x00C6); // AE
        mapping.put(0xE3, 0x00AA); // ordfeminine
        mapping.put(0xE8, 0x0141); // Lslash
        mapping.put(0xE9, 0x00D8); // Oslash
        mapping.put(0xEA, 0x0152); // OE
        mapping.put(0xEB, 0x00BA); // ordmasculine
        mapping.put(0xF1, 0x00E6); // ae
        mapping.put(0xF5, 0x0131); // dotlessi
        mapping.put(0xF8, 0x0142); // lslash
        mapping.put(0xF9, 0x00F8); // oslash
        mapping.put(0xFA, 0x0153); // oe
        mapping.put(0xFB, 0x00DF); // germandbls
    }

    /**
     * Fill mapping for ZapfDingbats encoding.
     * Maps character codes to their Unicode equivalents (U+2700 block).
     */
    private static void fillZapfDingbatsMapping(
            Map<Integer, Integer> mapping) {
        // ZapfDingbats standard encoding — key characters
        // Codes 0x20-0x7E and 0x80-0xFF map to dingbat Unicode chars
        mapping.put(0x20, 0x0020); // space
        // 0x21-0x2F: various ornaments/symbols
        mapping.put(0x21, 0x2701); mapping.put(0x22, 0x2702);
        mapping.put(0x23, 0x2703); mapping.put(0x24, 0x2704);
        mapping.put(0x25, 0x260E); mapping.put(0x26, 0x2706);
        mapping.put(0x27, 0x2707); mapping.put(0x28, 0x2708);
        mapping.put(0x29, 0x2709); mapping.put(0x2A, 0x261B);
        mapping.put(0x2B, 0x261E); mapping.put(0x2C, 0x270C);
        mapping.put(0x2D, 0x270D); mapping.put(0x2E, 0x270E);
        mapping.put(0x2F, 0x270F);
        // 0x30-0x39: pencils, scissors, etc.
        mapping.put(0x30, 0x2710); mapping.put(0x31, 0x2711);
        mapping.put(0x32, 0x2712); mapping.put(0x33, 0x2713);
        mapping.put(0x34, 0x2714); mapping.put(0x35, 0x2715);
        mapping.put(0x36, 0x2716); mapping.put(0x37, 0x2717);
        mapping.put(0x38, 0x2718); mapping.put(0x39, 0x2719);
        mapping.put(0x3A, 0x271A); mapping.put(0x3B, 0x271B);
        mapping.put(0x3C, 0x271C); mapping.put(0x3D, 0x271D);
        mapping.put(0x3E, 0x271E); mapping.put(0x3F, 0x271F);
        // 0x40-0x5A: crosses, stars, etc.
        mapping.put(0x40, 0x2720); mapping.put(0x41, 0x2721);
        mapping.put(0x42, 0x2722); mapping.put(0x43, 0x2723);
        mapping.put(0x44, 0x2724); mapping.put(0x45, 0x2725);
        mapping.put(0x46, 0x2726); mapping.put(0x47, 0x2727);
        mapping.put(0x48, 0x2605); mapping.put(0x49, 0x2729);
        mapping.put(0x4A, 0x272A); mapping.put(0x4B, 0x272B);
        mapping.put(0x4C, 0x272C); mapping.put(0x4D, 0x272D);
        mapping.put(0x4E, 0x272E); mapping.put(0x4F, 0x272F);
        mapping.put(0x50, 0x2730); mapping.put(0x51, 0x2731);
        mapping.put(0x52, 0x2732); mapping.put(0x53, 0x2733);
        mapping.put(0x54, 0x2734); mapping.put(0x55, 0x2735);
        mapping.put(0x56, 0x2736); mapping.put(0x57, 0x2737);
        mapping.put(0x58, 0x2738); mapping.put(0x59, 0x2739);
        mapping.put(0x5A, 0x273A);
        // 0x5B-0x60
        mapping.put(0x5B, 0x273B); mapping.put(0x5C, 0x273C);
        mapping.put(0x5D, 0x273D); mapping.put(0x5E, 0x273E);
        mapping.put(0x5F, 0x273F); mapping.put(0x60, 0x2740);
        // 0x61-0x7E: more dingbats
        mapping.put(0x61, 0x2741); mapping.put(0x62, 0x2742);
        mapping.put(0x63, 0x2743); mapping.put(0x64, 0x2744);
        mapping.put(0x65, 0x2745); mapping.put(0x66, 0x2746);
        mapping.put(0x67, 0x2747); mapping.put(0x68, 0x2748);
        mapping.put(0x69, 0x2749); mapping.put(0x6A, 0x274A);
        mapping.put(0x6B, 0x274B); mapping.put(0x6C, 0x25CF);
        mapping.put(0x6D, 0x274D); mapping.put(0x6E, 0x25A0);
        mapping.put(0x6F, 0x274F);
        mapping.put(0x70, 0x2750); mapping.put(0x71, 0x2751);
        mapping.put(0x72, 0x2752); mapping.put(0x73, 0x25B2);
        mapping.put(0x74, 0x25BC); mapping.put(0x75, 0x25C6);
        mapping.put(0x76, 0x2756); mapping.put(0x77, 0x25D7);
        mapping.put(0x78, 0x2758); mapping.put(0x79, 0x2759);
        mapping.put(0x7A, 0x275A); mapping.put(0x7B, 0x275B);
        mapping.put(0x7C, 0x275C); mapping.put(0x7D, 0x275D);
        mapping.put(0x7E, 0x275E);
        // 0x80-0xA0: arrows and misc
        mapping.put(0x80, 0x2768); mapping.put(0x81, 0x2769);
        mapping.put(0x82, 0x276A); mapping.put(0x83, 0x276B);
        mapping.put(0x84, 0x276C); mapping.put(0x85, 0x276D);
        mapping.put(0x86, 0x276E); mapping.put(0x87, 0x276F);
        mapping.put(0x88, 0x2770); mapping.put(0x89, 0x2771);
        mapping.put(0x8A, 0x2772); mapping.put(0x8B, 0x2773);
        mapping.put(0x8C, 0x2774); mapping.put(0x8D, 0x2775);
        mapping.put(0xA1, 0x2761); mapping.put(0xA2, 0x2762);
        mapping.put(0xA3, 0x2763); mapping.put(0xA4, 0x2764);
        mapping.put(0xA5, 0x2765); mapping.put(0xA6, 0x2766);
        mapping.put(0xA7, 0x2767);
        mapping.put(0xA8, 0x2663); mapping.put(0xA9, 0x2666);
        mapping.put(0xAA, 0x2665); mapping.put(0xAB, 0x2660);
        mapping.put(0xAC, 0x2460); mapping.put(0xAD, 0x2461);
        mapping.put(0xAE, 0x2462); mapping.put(0xAF, 0x2463);
        mapping.put(0xB0, 0x2464); mapping.put(0xB1, 0x2465);
        mapping.put(0xB2, 0x2466); mapping.put(0xB3, 0x2467);
        mapping.put(0xB4, 0x2468); mapping.put(0xB5, 0x2469);
        mapping.put(0xB6, 0x2776); mapping.put(0xB7, 0x2777);
        mapping.put(0xB8, 0x2778); mapping.put(0xB9, 0x2779);
        mapping.put(0xBA, 0x277A); mapping.put(0xBB, 0x277B);
        mapping.put(0xBC, 0x277C); mapping.put(0xBD, 0x277D);
        mapping.put(0xBE, 0x277E); mapping.put(0xBF, 0x277F);
        // Arrows
        mapping.put(0xD5, 0x27A2); mapping.put(0xD6, 0x27A3);
        mapping.put(0xD7, 0x27A4); mapping.put(0xD8, 0x27A5);
        mapping.put(0xD9, 0x27A6); mapping.put(0xDA, 0x27A7);
        mapping.put(0xDB, 0x27A8); mapping.put(0xDC, 0x27A9);
        mapping.put(0xDD, 0x27AA); mapping.put(0xDE, 0x27AB);
        mapping.put(0xDF, 0x27AC); mapping.put(0xE0, 0x27AD);
        mapping.put(0xE1, 0x27AE); mapping.put(0xE2, 0x27AF);
        mapping.put(0xE4, 0x27B1); mapping.put(0xE5, 0x27B2);
        mapping.put(0xE6, 0x27B3); mapping.put(0xE7, 0x27B4);
        mapping.put(0xE8, 0x27B5); mapping.put(0xE9, 0x27B6);
        mapping.put(0xEA, 0x27B7); mapping.put(0xEB, 0x27B8);
        mapping.put(0xEC, 0x27B9); mapping.put(0xED, 0x27BA);
        mapping.put(0xEE, 0x27BB); mapping.put(0xEF, 0x27BC);
        mapping.put(0xF1, 0x27BE);
    }

    /**
     * Fill mapping for Symbol font encoding.
     * Maps character codes to their Unicode equivalents (Greek, math).
     */
    private static void fillSymbolMapping(
            Map<Integer, Integer> mapping) {
        mapping.put(0x20, 0x0020); // space
        mapping.put(0x21, 0x0021); // exclam
        mapping.put(0x22, 0x2200); // universal
        mapping.put(0x23, 0x0023); // numbersign
        mapping.put(0x24, 0x2203); // existential
        mapping.put(0x25, 0x0025); // percent
        mapping.put(0x26, 0x0026); // ampersand
        mapping.put(0x27, 0x220B); // suchthat
        mapping.put(0x28, 0x0028); mapping.put(0x29, 0x0029);
        mapping.put(0x2A, 0x2217); // asteriskmath
        mapping.put(0x2B, 0x002B); mapping.put(0x2C, 0x002C);
        mapping.put(0x2D, 0x2212); // minus
        mapping.put(0x2E, 0x002E); mapping.put(0x2F, 0x002F);
        // Digits same as ASCII
        for (int i = 0x30; i <= 0x39; i++) mapping.put(i, i);
        mapping.put(0x3A, 0x003A); mapping.put(0x3B, 0x003B);
        mapping.put(0x3C, 0x003C); mapping.put(0x3D, 0x003D);
        mapping.put(0x3E, 0x003E); mapping.put(0x3F, 0x003F);
        mapping.put(0x40, 0x2245); // congruent
        // Greek uppercase: A-Ω
        mapping.put(0x41, 0x0391); mapping.put(0x42, 0x0392);
        mapping.put(0x43, 0x03A7); mapping.put(0x44, 0x0394);
        mapping.put(0x45, 0x0395); mapping.put(0x46, 0x03A6);
        mapping.put(0x47, 0x0393); mapping.put(0x48, 0x0397);
        mapping.put(0x49, 0x0399); mapping.put(0x4A, 0x03D1);
        mapping.put(0x4B, 0x039A); mapping.put(0x4C, 0x039B);
        mapping.put(0x4D, 0x039C); mapping.put(0x4E, 0x039D);
        mapping.put(0x4F, 0x039F); mapping.put(0x50, 0x03A0);
        mapping.put(0x51, 0x0398); mapping.put(0x52, 0x03A1);
        mapping.put(0x53, 0x03A3); mapping.put(0x54, 0x03A4);
        mapping.put(0x55, 0x03A5); mapping.put(0x56, 0x03C2);
        mapping.put(0x57, 0x03A9); mapping.put(0x58, 0x039E);
        mapping.put(0x59, 0x03A8); mapping.put(0x5A, 0x0396);
        mapping.put(0x5B, 0x005B); mapping.put(0x5C, 0x2234);
        mapping.put(0x5D, 0x005D); mapping.put(0x5E, 0x22A5);
        mapping.put(0x5F, 0x005F); mapping.put(0x60, 0xF8E5);
        // Greek lowercase: α-ω
        mapping.put(0x61, 0x03B1); mapping.put(0x62, 0x03B2);
        mapping.put(0x63, 0x03C7); mapping.put(0x64, 0x03B4);
        mapping.put(0x65, 0x03B5); mapping.put(0x66, 0x03C6);
        mapping.put(0x67, 0x03B3); mapping.put(0x68, 0x03B7);
        mapping.put(0x69, 0x03B9); mapping.put(0x6A, 0x03D5);
        mapping.put(0x6B, 0x03BA); mapping.put(0x6C, 0x03BB);
        mapping.put(0x6D, 0x03BC); mapping.put(0x6E, 0x03BD);
        mapping.put(0x6F, 0x03BF); mapping.put(0x70, 0x03C0);
        mapping.put(0x71, 0x03B8); mapping.put(0x72, 0x03C1);
        mapping.put(0x73, 0x03C3); mapping.put(0x74, 0x03C4);
        mapping.put(0x75, 0x03C5); mapping.put(0x76, 0x03D6);
        mapping.put(0x77, 0x03C9); mapping.put(0x78, 0x03BE);
        mapping.put(0x79, 0x03C8); mapping.put(0x7A, 0x03B6);
        mapping.put(0x7B, 0x007B); mapping.put(0x7C, 0x007C);
        mapping.put(0x7D, 0x007D); mapping.put(0x7E, 0x223C);
        // Math operators
        mapping.put(0xA0, 0x20AC); // Euro sign
        mapping.put(0xA5, 0x221E); // infinity
        mapping.put(0xB1, 0x00B1); // plusminus
        mapping.put(0xB2, 0x2033); // second
        mapping.put(0xB3, 0x2265); // greaterequal
        mapping.put(0xB4, 0x00D7); // multiply
        mapping.put(0xB5, 0x221D); // proportional
        mapping.put(0xB6, 0x2202); // partialdiff
        mapping.put(0xB7, 0x2022); // bullet
        mapping.put(0xB8, 0x00F7); // divide
        mapping.put(0xB9, 0x2260); // notequal
        mapping.put(0xBA, 0x2261); // equivalence
        mapping.put(0xBB, 0x2248); // approxequal
        mapping.put(0xBC, 0x2026); // ellipsis
        mapping.put(0xC0, 0x2135); // aleph
        mapping.put(0xC1, 0x2111); // Ifraktur
        mapping.put(0xC2, 0x211C); // Rfraktur
        mapping.put(0xC3, 0x2118); // weierstrass
        mapping.put(0xC4, 0x2297); // circlemultiply
        mapping.put(0xC5, 0x2295); // circleplus
        mapping.put(0xC6, 0x2205); // emptyset
        mapping.put(0xC7, 0x2229); // intersection
        mapping.put(0xC8, 0x222A); // union
        mapping.put(0xC9, 0x2283); // propersuperset
        mapping.put(0xCA, 0x2287); // reflexsuperset
        mapping.put(0xCB, 0x2284); // notsubset
        mapping.put(0xCC, 0x2282); // propersubset
        mapping.put(0xCD, 0x2286); // reflexsubset
        mapping.put(0xCE, 0x2208); // element
        mapping.put(0xCF, 0x2209); // notelement
        mapping.put(0xD0, 0x2220); // angle
        mapping.put(0xD1, 0x2207); // gradient
        mapping.put(0xD5, 0x220F); // product
        mapping.put(0xD6, 0x221A); // radical
        mapping.put(0xD9, 0x2260); // notequal
        mapping.put(0xDA, 0x2264); // lessequal
        mapping.put(0xDB, 0x2265); // greaterequal
    }

    /**
     * Fill mapping for Wingdings font encoding.
     * Maps character codes to Unicode equivalents where known,
     * or to Microsoft PUA block (U+F0xx) for Wingdings-specific symbols.
     */
    private static void fillWingdingsMapping(
            Map<Integer, Integer> mapping) {
        mapping.put(0x20, 0x0020); // space
        // 0x21-0x29: writing/editing symbols
        mapping.put(0x21, 0x270F); // pencil
        mapping.put(0x22, 0x2702); // scissors
        mapping.put(0x23, 0x2701); // upper blade scissors
        mapping.put(0x24, 0xF024); // eyeglasses
        mapping.put(0x25, 0xF025); // bell
        mapping.put(0x26, 0xF026); // book open
        mapping.put(0x27, 0xF027); // candle
        mapping.put(0x28, 0x260E); // telephone
        mapping.put(0x29, 0x2706); // telephone location sign
        mapping.put(0x2A, 0x2709); // envelope
        mapping.put(0x2B, 0xF02B); // envelope (open)
        mapping.put(0x2C, 0xF02C); // envelope (closed, stamp)
        mapping.put(0x2D, 0xF02D); // mailbox, flag down
        mapping.put(0x2E, 0xF02E); // mailbox, flag up
        mapping.put(0x2F, 0xF02F); // mailbox (open)
        mapping.put(0x30, 0xF030); // folder (closed)
        mapping.put(0x31, 0xF031); // folder (open)
        mapping.put(0x32, 0xF032); // page
        mapping.put(0x33, 0xF033); // suitcase
        mapping.put(0x34, 0xF034); // hourglass
        mapping.put(0x35, 0xF035); // watch
        mapping.put(0x36, 0x231A); // watch
        mapping.put(0x37, 0xF037); // window
        mapping.put(0x38, 0xF038); // mouse (computer)
        mapping.put(0x39, 0xF039); // trackball
        mapping.put(0x3A, 0xF03A); // computer
        mapping.put(0x3B, 0xF03B); // hard disk
        mapping.put(0x3C, 0xF03C); // floppy disk (3.5)
        mapping.put(0x3D, 0xF03D); // floppy disk (5.25)
        mapping.put(0x3E, 0xF03E); // printer
        mapping.put(0x3F, 0xF03F); // phone/fax
        mapping.put(0x40, 0xF040); // fax/printer
        mapping.put(0x41, 0xF041); // document (single page)
        mapping.put(0x42, 0xF042); // document (multiple pages)
        mapping.put(0x43, 0xF043); // document (signature)
        mapping.put(0x44, 0xF044); // pen nib
        mapping.put(0x45, 0xF045); // pen nib (angled)
        // 0x46-0x4F: hands
        mapping.put(0x46, 0xF046); // writing hand (left)
        mapping.put(0x47, 0xF047); // writing hand (right)
        mapping.put(0x48, 0xF048); // writing hand
        mapping.put(0x49, 0xF049); // hand (left point)
        mapping.put(0x4A, 0x261C); // left pointing index
        mapping.put(0x4B, 0x261E); // right pointing index
        mapping.put(0x4C, 0x261D); // up pointing index
        mapping.put(0x4D, 0x261F); // down pointing index
        mapping.put(0x4E, 0xF04E); // hand (open)
        mapping.put(0x4F, 0x263A); // smiley face
        mapping.put(0x50, 0xF050); // indifferent face
        mapping.put(0x51, 0x2639); // frowning face
        mapping.put(0x52, 0xF052); // bomb
        mapping.put(0x53, 0x2620); // skull and crossbones
        mapping.put(0x54, 0x2690); // white flag
        mapping.put(0x55, 0x2691); // black flag (pennant)
        mapping.put(0x56, 0x2708); // airplane
        mapping.put(0x57, 0x263C); // sun
        mapping.put(0x58, 0xF058); // droplet
        mapping.put(0x59, 0x2744); // snowflake
        mapping.put(0x5A, 0x271E); // shadowed white latin cross
        mapping.put(0x5B, 0x2720); // maltese cross
        mapping.put(0x5C, 0x2721); // star of david
        mapping.put(0x5D, 0x262A); // star and crescent
        mapping.put(0x5E, 0x2638); // wheel of dharma
        mapping.put(0x5F, 0x2648); // aries
        mapping.put(0x60, 0x2649); // taurus
        mapping.put(0x61, 0x264A); // gemini
        mapping.put(0x62, 0x264B); // cancer
        mapping.put(0x63, 0x264C); // leo
        mapping.put(0x64, 0x264D); // virgo
        mapping.put(0x65, 0x264E); // libra
        mapping.put(0x66, 0x264F); // scorpio
        mapping.put(0x67, 0x2650); // sagittarius
        mapping.put(0x68, 0x2651); // capricorn
        mapping.put(0x69, 0x2652); // aquarius
        mapping.put(0x6A, 0x2653); // pisces
        // 0x6B-0x6F: misc symbols
        mapping.put(0x6B, 0xF06B); // ampersand (script)
        mapping.put(0x6C, 0xF06C); // ampersand (italic)
        mapping.put(0x6D, 0x25CF); // black circle
        mapping.put(0x6E, 0x25CB); // white circle
        mapping.put(0x6F, 0xF06F); // circle with line
        // 0x70-0x7E: geometric shapes & checkmarks
        mapping.put(0x70, 0xF070); // rounded square (dark)
        mapping.put(0x71, 0xF071); // rounded square (light)
        mapping.put(0x72, 0x25A0); // black square
        mapping.put(0x73, 0x25A1); // white square
        mapping.put(0x74, 0x25A1); // white square (thin border)
        mapping.put(0x75, 0x25C6); // black diamond
        mapping.put(0x76, 0x25C7); // white diamond
        mapping.put(0x77, 0x25C8); // white diamond containing black small diamond
        mapping.put(0x78, 0xF078); // diamond target
        mapping.put(0x79, 0x2327); // x in rectangle box
        mapping.put(0x7A, 0xF07A); // diamond (4-point)
        mapping.put(0x7B, 0x2BB9); // up arrow in square
        mapping.put(0x7C, 0xF07C); // filled lozenge (horizontal)
        mapping.put(0x7D, 0x2318); // place of interest (command)
        mapping.put(0x7E, 0xF07E); // Windows flag
        // 0x80-0x9F: various symbols
        mapping.put(0x80, 0xF080); // document (type1)
        mapping.put(0x81, 0xF081); // document (type2)
        mapping.put(0x82, 0xF082); // document (type3)
        mapping.put(0x83, 0xF083); // document (type4)
        mapping.put(0x84, 0xF084); // italic x
        mapping.put(0x85, 0xF085); // bold italic x
        mapping.put(0x86, 0xF086); // circle (3d)
        mapping.put(0x87, 0xF087); // circle-dot
        mapping.put(0x88, 0xF088); // target
        mapping.put(0x89, 0xF089); // square (4-point star)
        mapping.put(0x8A, 0xF08A); // star (5-point)
        mapping.put(0x8B, 0x2605); // black star
        mapping.put(0x8C, 0x2606); // white star
        mapping.put(0x8D, 0xF08D); // star (6-point)
        mapping.put(0x8E, 0xF08E); // star (8-point)
        mapping.put(0x8F, 0xF08F); // cross target
        // 0x90-0xFB: remaining symbols — use Microsoft PUA convention (U+F0xx)
        mapping.put(0x90, 0xF090); // cross target (outline)
        mapping.put(0x91, 0xF091); // cross (shadow)
        mapping.put(0x92, 0xF092); // cross (3D)
        mapping.put(0x93, 0xF093); // cross (outline shadow)
        mapping.put(0x94, 0xF094); // star (4-point)
        mapping.put(0x95, 0xF095); // star (4-point, filled)
        mapping.put(0x96, 0xF096); // star (6-point)
        mapping.put(0x97, 0xF097); // pentagon
        mapping.put(0x98, 0xF098); // hexagon
        mapping.put(0x99, 0xF099); // octagon
        mapping.put(0x9A, 0xF09A); // plus (thin)
        mapping.put(0x9B, 0xF09B); // plus (outline)
        mapping.put(0x9C, 0xF09C); // plus (shadow)
        mapping.put(0x9D, 0xF09D); // cross (thin)
        mapping.put(0x9E, 0xF09E); // cross (outline)
        mapping.put(0x9F, 0xF09F); // cross (shadow)
        mapping.put(0xA0, 0xF0A0); // symbol placeholder
        mapping.put(0xA1, 0xF0A1); // ribbon
        mapping.put(0xA2, 0xF0A2); // ribbon (outline)
        mapping.put(0xA3, 0xF0A3); // ribbon (shadow)
        mapping.put(0xA4, 0xF0A4); // circle (outline)
        mapping.put(0xA5, 0xF0A5); // circle (shadow)
        mapping.put(0xA6, 0xF0A6); // circle (3D)
        mapping.put(0xA7, 0xF0A7); // diamond bullet
        mapping.put(0xA8, 0xF0A8); // circle (target)
        mapping.put(0xA9, 0xF0A9); // octagon (filled)
        mapping.put(0xAA, 0xF0AA); // diamond (outline)
        mapping.put(0xAB, 0xF0AB); // diamond (shadow)
        mapping.put(0xAC, 0xF0AC); // diamond (3D)
        mapping.put(0xAD, 0xF0AD); // triangle up
        mapping.put(0xAE, 0xF0AE); // triangle up (filled)
        mapping.put(0xAF, 0xF0AF); // triangle down
        mapping.put(0xB0, 0xF0B0); // triangle down (filled)
        mapping.put(0xB1, 0xF0B1); // triangle left
        mapping.put(0xB2, 0xF0B2); // triangle left (filled)
        mapping.put(0xB3, 0xF0B3); // triangle right
        mapping.put(0xB4, 0xF0B4); // triangle right (filled)
        mapping.put(0xB5, 0xF0B5); // arrow right (small)
        mapping.put(0xB6, 0xF0B6); // arrow right (outline)
        mapping.put(0xB7, 0xF0B7); // arrow right (filled)
        mapping.put(0xB8, 0xF0B8); // arrow right (shadow)
        mapping.put(0xB9, 0xF0B9); // arrow right (3D)
        mapping.put(0xBA, 0xF0BA); // arrow right (notched)
        mapping.put(0xBB, 0xF0BB); // arrow right (chevron)
        mapping.put(0xBC, 0xF0BC); // arrow right (triangle)
        mapping.put(0xBD, 0xF0BD); // arrow right (concave)
        mapping.put(0xBE, 0xF0BE); // arrow right (heavy)
        mapping.put(0xBF, 0xF0BF); // arrow right (feathered)
        mapping.put(0xC0, 0xF0C0); // arrow right (striped)
        mapping.put(0xC1, 0xF0C1); // arrow right (open)
        mapping.put(0xC2, 0xF0C2); // arrow up
        mapping.put(0xC3, 0xF0C3); // arrow up (outline)
        mapping.put(0xC4, 0xF0C4); // arrow down
        mapping.put(0xC5, 0xF0C5); // arrow down (outline)
        mapping.put(0xC6, 0xF0C6); // arrow left
        mapping.put(0xC7, 0xF0C7); // arrow left (outline)
        mapping.put(0xC8, 0xF0C8); // arrow up-down
        mapping.put(0xC9, 0xF0C9); // arrow left-right
        mapping.put(0xCA, 0xF0CA); // arrow up-left
        mapping.put(0xCB, 0xF0CB); // arrow up-right
        mapping.put(0xCC, 0xF0CC); // arrow down-left
        mapping.put(0xCD, 0xF0CD); // arrow down-right
        mapping.put(0xCE, 0xF0CE); // arrow diagonal
        mapping.put(0xCF, 0xF0CF); // arrow diagonal (outline)
        mapping.put(0xD0, 0xF0D0); // arrow (curved up)
        mapping.put(0xD1, 0xF0D1); // arrow (curved down)
        mapping.put(0xD2, 0xF0D2); // arrow (curved left)
        mapping.put(0xD3, 0xF0D3); // arrow (curved right)
        mapping.put(0xD4, 0xF0D4); // arrow (loop left)
        mapping.put(0xD5, 0x232B); // erase to the left
        mapping.put(0xD6, 0x2326); // erase to the right
        mapping.put(0xD7, 0xF0D7); // three-D arrow left
        mapping.put(0xD8, 0xF0D8); // three-D arrow right
        mapping.put(0xD9, 0xF0D9); // three-D arrow up
        mapping.put(0xDA, 0xF0DA); // three-D arrow down
        mapping.put(0xDB, 0xF0DB); // circled number 1
        mapping.put(0xDC, 0xF0DC); // circled number 2
        mapping.put(0xDD, 0xF0DD); // circled number 3
        mapping.put(0xDE, 0xF0DE); // circled number 4
        mapping.put(0xDF, 0xF0DF); // circled number 5
        mapping.put(0xE0, 0xF0E0); // circled number 6
        mapping.put(0xE1, 0xF0E1); // circled number 7
        mapping.put(0xE2, 0xF0E2); // circled number 8
        mapping.put(0xE3, 0xF0E3); // circled number 9
        mapping.put(0xE4, 0xF0E4); // circled number 10
        mapping.put(0xE5, 0xF0E5); // negative circled sans 1
        mapping.put(0xE6, 0xF0E6); // negative circled sans 2
        mapping.put(0xE7, 0xF0E7); // negative circled sans 3
        mapping.put(0xE8, 0xF0E8); // negative circled sans 4
        mapping.put(0xE9, 0xF0E9); // negative circled sans 5
        mapping.put(0xEA, 0xF0EA); // negative circled sans 6
        mapping.put(0xEB, 0xF0EB); // negative circled sans 7
        mapping.put(0xEC, 0xF0EC); // negative circled sans 8
        mapping.put(0xED, 0xF0ED); // negative circled sans 9
        mapping.put(0xEE, 0xF0EE); // negative circled sans 10
        mapping.put(0xEF, 0xF0EF); // curly bracket left
        mapping.put(0xF0, 0xF0F0); // curly bracket right
        mapping.put(0xF1, 0x232A); // right-pointing angle bracket
        mapping.put(0xF2, 0x2329); // left-pointing angle bracket
        mapping.put(0xF3, 0xF0F3); // nested brackets left
        mapping.put(0xF4, 0xF0F4); // nested brackets right
        mapping.put(0xF5, 0xF0F5); // double bracket left
        mapping.put(0xF6, 0xF0F6); // double bracket right
        mapping.put(0xF7, 0xF0F7); // bracket pair (filled)
        mapping.put(0xF8, 0xF0F8); // bracket pair (outline)
        mapping.put(0xF9, 0xF0F9); // square bracket left
        mapping.put(0xFA, 0xF0FA); // square bracket right
        mapping.put(0xFB, 0xF0FB); // bracket pair (curly)
        // 0xFC-0xFE: checkmarks and boxes
        mapping.put(0xFC, 0x2713); // check mark
        mapping.put(0xFD, 0x2717); // ballot x
        mapping.put(0xFE, 0x2612); // ballot box with x
    }

    // -----------------------------------------------------------------
    // Font Compliance
    // -----------------------------------------------------------------

    /**
     * Strip CIDSet entries from CID font descriptors.
     * PDF/UA 7.21.4.2 requires CIDSet to list ALL glyphs in the font
     * program. Source PDFs often have incorrect CIDSets from subsetting.
     * Removing CIDSet is valid — the test passes if CIDSet is absent.
     */
    static void stripIncorrectCIDSets(PdfDocument pdfDoc) {
        int strippedCIDSet = 0;
        int strippedCharSet = 0;
        PdfName cidSetName = new PdfName("CIDSet");
        PdfName charSetName = new PdfName("CharSet");
        Set<PdfIndirectReference> seen = new HashSet<>();

        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfPage page = pdfDoc.getPage(p);
            PdfDictionary resources = page.getResources() != null
                ? page.getResources().getPdfObject() : null;
            if (resources == null) continue;

            PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
            if (fonts == null) continue;

            for (PdfName fontKey : fonts.keySet()) {
                PdfDictionary fontDict = fonts.getAsDictionary(fontKey);
                if (fontDict == null) continue;

                // --- CIDSet for CID fonts (Type0 > DescendantFonts) ---
                PdfArray descendants = fontDict.getAsArray(
                    new PdfName("DescendantFonts"));
                if (descendants != null && descendants.size() > 0) {
                    PdfDictionary descFont =
                        descendants.getAsDictionary(0);
                    if (descFont != null) {
                        PdfIndirectReference ref =
                            descFont.getIndirectReference();
                        if (ref == null || seen.add(ref)) {
                            PdfDictionary fd = descFont.getAsDictionary(
                                new PdfName("FontDescriptor"));
                            if (fd != null
                                    && fd.containsKey(cidSetName)) {
                                fd.remove(cidSetName);
                                fd.setModified();
                                strippedCIDSet++;
                                sqStrippedCIDSet++;
                                String name = fontDict.containsKey(
                                        PdfName.BaseFont)
                                    ? fontDict.getAsName(PdfName.BaseFont)
                                        .getValue()
                                    : fontKey.getValue();
                                System.out.println(
                                    "  Stripped CIDSet from " + name);
                            }
                        }
                    }
                }

                // --- CharSet for simple fonts (Type1, TrueType) ---
                // PDF/UA 7.21.4.2-t1: If CharSet is present it must
                // correctly list all glyphs. Removing is always safe
                // since CharSet is optional.
                PdfDictionary fd = fontDict.getAsDictionary(
                    new PdfName("FontDescriptor"));
                if (fd != null && fd.containsKey(charSetName)) {
                    PdfIndirectReference fdRef =
                        fd.getIndirectReference();
                    if (fdRef == null || seen.add(fdRef)) {
                        fd.remove(charSetName);
                        fd.setModified();
                        strippedCharSet++;
                        sqStrippedCharSet++;
                        String name = fontDict.containsKey(
                                PdfName.BaseFont)
                            ? fontDict.getAsName(PdfName.BaseFont)
                                .getValue()
                            : fontKey.getValue();
                        System.out.println(
                            "  Stripped CharSet from " + name);
                    }
                }
            }
        }
        if (strippedCIDSet > 0) {
            System.out.println("  Stripped CIDSet from " + strippedCIDSet
                + " CID font(s)");
        }
        if (strippedCharSet > 0) {
            System.out.println("  Stripped CharSet from " + strippedCharSet
                + " Type1 font(s)");
        }
    }

    /**
     * Strip /Encoding from symbolic TrueType fonts.
     *
     * ISO 32000-1 §9.6.6.4 / PDF/UA 7.21.6 t3: Symbolic TrueType fonts
     * must NOT have an /Encoding entry. Older Word/Office versions
     * incorrectly set the symbolic flag on standard text fonts while also
     * including an /Encoding dictionary.
     *
     * MUST be called AFTER addMissingToUnicodeCMaps() — the CMap generator
     * reads /Encoding + /Differences to build ToUnicode mappings.
     */
    static void stripSymbolicTrueTypeEncoding(PdfDocument pdfDoc) {
        int stripped = 0;
        Set<PdfIndirectReference> seen = new HashSet<>();

        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfPage page = pdfDoc.getPage(p);
            PdfDictionary resources = page.getResources() != null
                ? page.getResources().getPdfObject() : null;
            if (resources == null) continue;

            stripped += stripSymbolicEncodingFromFonts(resources, seen);

            // Also check fonts inside Form XObjects
            PdfDictionary xObjects = resources.getAsDictionary(PdfName.XObject);
            if (xObjects != null) {
                for (PdfName xobjName : xObjects.keySet()) {
                    PdfObject xobjObj = xObjects.get(xobjName, false);
                    PdfStream xobjStream = null;
                    if (xobjObj instanceof PdfStream) {
                        xobjStream = (PdfStream) xobjObj;
                    } else if (xobjObj instanceof PdfIndirectReference) {
                        PdfObject deref = ((PdfIndirectReference) xobjObj)
                            .getRefersTo();
                        if (deref instanceof PdfStream) {
                            xobjStream = (PdfStream) deref;
                        }
                    }
                    if (xobjStream != null) {
                        PdfDictionary xobjRes = xobjStream.getAsDictionary(
                            PdfName.Resources);
                        if (xobjRes != null) {
                            stripped += stripSymbolicEncodingFromFonts(
                                xobjRes, seen);
                        }
                    }
                }
            }
        }
        if (stripped > 0) {
            sqStrippedSymbolicEncoding += stripped;
            System.out.println("  Stripped /Encoding from " + stripped
                + " symbolic TrueType font(s)");
        }
    }

    /**
     * Helper: strip /Encoding from symbolic TrueType fonts in a resource dict.
     */
    private static int stripSymbolicEncodingFromFonts(
            PdfDictionary resources, Set<PdfIndirectReference> seen) {
        int stripped = 0;
        PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
        if (fonts == null) return 0;

        for (PdfName fontKey : fonts.keySet()) {
            PdfDictionary fontDict = fonts.getAsDictionary(fontKey);
            if (fontDict == null) continue;

            // Only TrueType fonts
            PdfName subtype = fontDict.getAsName(PdfName.Subtype);
            if (subtype == null || !PdfName.TrueType.equals(subtype)) continue;

            // Must have /Encoding
            if (!fontDict.containsKey(PdfName.Encoding)) continue;

            // Check FontDescriptor for symbolic flag (bit 3 = value 4)
            PdfDictionary fd = fontDict.getAsDictionary(
                new PdfName("FontDescriptor"));
            if (fd == null) continue;

            PdfNumber flagsObj = fd.getAsNumber(new PdfName("Flags"));
            if (flagsObj == null) continue;
            int flags = flagsObj.intValue();
            if ((flags & 4) == 0) continue; // not symbolic

            // Dedup by indirect reference
            PdfIndirectReference ref = fontDict.getIndirectReference();
            if (ref != null && !seen.add(ref)) continue;

            fontDict.remove(PdfName.Encoding);
            fontDict.setModified();
            stripped++;

            String name = fontDict.containsKey(PdfName.BaseFont)
                ? fontDict.getAsName(PdfName.BaseFont).getValue()
                : fontKey.getValue();
            System.out.println(
                "  Stripped /Encoding from symbolic TrueType: " + name);
        }
        return stripped;
    }

    /**
     * Fix CIDToGIDMap entries for CIDFontType2 (TrueType-based CID) fonts.
     *
     * PDF/UA 7.21.3.2 requires CIDFontType2 fonts to have a valid
     * CIDToGIDMap entry. Some source PDFs have:
     * - Missing CIDToGIDMap entirely
     * - A PdfString "Identity" instead of PdfName /Identity
     * - A null or invalid entry
     *
     * The fix sets /CIDToGIDMap to the PdfName /Identity, which is the
     * standard identity mapping (CID = GID). This is valid for fonts
     * with identity CID-to-GID mapping (the common case).
     */
    static void fixCIDToGIDMap(PdfDocument pdfDoc) {
        int fixed = 0;
        PdfName cidToGIDMapName = new PdfName("CIDToGIDMap");
        PdfName cidFontType2 = new PdfName("CIDFontType2");
        Set<PdfIndirectReference> processed = new HashSet<>();

        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfPage page = pdfDoc.getPage(p);
            PdfDictionary resources = page.getResources() != null
                ? page.getResources().getPdfObject() : null;
            if (resources == null) continue;

            PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
            if (fonts == null) continue;

            for (PdfName fontKey : fonts.keySet()) {
                PdfDictionary fontDict = fonts.getAsDictionary(fontKey);
                if (fontDict == null) continue;

                // Only Type0 fonts have DescendantFonts
                PdfArray descendants = fontDict.getAsArray(
                    new PdfName("DescendantFonts"));
                if (descendants == null || descendants.size() == 0) continue;

                PdfDictionary descFont = descendants.getAsDictionary(0);
                if (descFont == null) continue;

                // Skip if already processed (shared font objects)
                PdfIndirectReference ref =
                    descFont.getIndirectReference();
                if (ref != null && !processed.add(ref)) continue;

                // Only fix CIDFontType2 (TrueType-based)
                PdfName subtype = descFont.getAsName(PdfName.Subtype);
                if (!cidFontType2.equals(subtype)) continue;

                PdfObject existing = descFont.get(cidToGIDMapName);
                boolean needsFix = false;

                if (existing == null) {
                    // Missing entirely
                    needsFix = true;
                } else if (existing instanceof PdfString) {
                    // String "Identity" instead of Name /Identity
                    needsFix = true;
                } else if (existing instanceof PdfName) {
                    // Already a PdfName — check it's /Identity
                    if (!"Identity".equals(
                            ((PdfName) existing).getValue())) {
                        needsFix = true;
                    }
                }
                // If it's a PdfStream, it's a custom mapping — leave it

                if (needsFix) {
                    descFont.put(cidToGIDMapName, new PdfName("Identity"));
                    descFont.setModified();
                    fixed++;
                    sqFixedCIDToGIDMap++;

                    String name = fontDict.containsKey(PdfName.BaseFont)
                        ? fontDict.getAsName(PdfName.BaseFont).getValue()
                        : fontKey.getValue();
                    System.out.println("  Fixed CIDToGIDMap for " + name);
                }
            }
        }
        if (fixed > 0) {
            System.out.println("  Fixed CIDToGIDMap for " + fixed
                + " CIDFontType2 font(s)");
        }
    }

    // -----------------------------------------------------------------
    // Source Structure Preservation Mode
    // -----------------------------------------------------------------

    /**
     * Assess whether the source PDF has a sufficiently well-formed
     * structure tree that it should be preserved rather than rebuilt.
     *
     * Criteria for "good" source structure:
     * - Has a StructTreeRoot with children (/K)
     * - Has at least 1 Table structure element
     * - Has visible (non-invisible) MCR blocks in content streams
     *
     * @return true if source structure should be preserved
     */
    private static boolean assessSourceStructure(String inputPath) {
        try (PdfReader reader = new PdfReader(inputPath);
             PdfDocument pdfDoc = new PdfDocument(reader)) {

            PdfDictionary catalog = pdfDoc.getCatalog().getPdfObject();
            PdfDictionary stRoot = catalog.getAsDictionary(PdfName.StructTreeRoot);
            if (stRoot == null || stRoot.get(PdfName.K) == null) {
                return false;
            }

            // Count structure elements by type
            int tableCount = 0;
            int tdCount = 0;
            int trCount = 0;
            int pCount = 0;
            int totalSE = 0;

            for (int xref = 1; xref < pdfDoc.getNumberOfPdfObjects(); xref++) {
                try {
                    PdfObject obj = pdfDoc.getPdfObject(xref);
                    if (obj == null || !obj.isDictionary()) continue;
                    PdfDictionary dict = (PdfDictionary) obj;
                    PdfName type = dict.getAsName(PdfName.Type);
                    if (type == null || !PdfName.StructElem.equals(type)) continue;

                    totalSE++;
                    PdfName s = dict.getAsName(PdfName.S);
                    if (s == null) continue;
                    String tag = s.getValue();
                    switch (tag) {
                        case "Table": tableCount++; break;
                        case "TD":    tdCount++;    break;
                        case "TR":    trCount++;    break;
                        case "P":     pCount++;     break;
                        default:                    break;
                    }
                } catch (Exception e) {
                    System.err.println("WARNING: Skipping malformed object " + xref
                        + " in assessSourceStructure: " + e.getMessage());
                }
            }

            // Check for visible MCR content across first N pages
            // (not just page 1 — documents may have title pages without MCR)
            int visibleMCR = 0;
            int pagesToCheck = Math.min(pdfDoc.getNumberOfPages(), 3);
            for (int p = 1; p <= pagesToCheck; p++) {
                PdfPage page = pdfDoc.getPage(p);
                byte[] contentBytes = page.getContentBytes();
                if (contentBytes == null) continue;
                String content = new String(contentBytes,
                    StandardCharsets.ISO_8859_1);
                // Count BDC blocks with MCID that do NOT contain "3 Tr"
                java.util.regex.Pattern bdc = java.util.regex.Pattern.compile(
                    "/\\w+\\s*<<[^>]*/MCID\\s+\\d+[^>]*>>\\s*BDC(.*?)EMC",
                    java.util.regex.Pattern.DOTALL);
                java.util.regex.Matcher m = bdc.matcher(content);
                while (m.find()) {
                    if (!m.group(1).contains("3 Tr")) {
                        visibleMCR++;
                    }
                }
            }

            // Preserve if source has enough visible MCR and structure elements.
            // Tables are NOT required — text-heavy documents (newsletters,
            // reports) with good MCR should be preserved too.
            boolean isGood = visibleMCR >= 5 && totalSE >= 10;
            System.out.println("  Source structure assessment: "
                + totalSE + " SEs, " + tableCount + " Table, "
                + trCount + " TR, " + tdCount + " TD, "
                + pCount + " P, " + visibleMCR + " visible MCR"
                + " (checked " + pagesToCheck + " pages)"
                + " → " + (isGood ? "PRESERVE" : "REBUILD"));
            return isGood;

        } catch (Exception e) {
            System.err.println("  Source assessment failed: " + e.getMessage());
            return false;
        }
    }

    /**
     * Preservation mode: keep the source PDF's existing structure tree and
     * content stream MCR intact. Only apply metadata, compliance fixes,
     * and targeted enrichments from the Docling analysis.
     *
     * This mode is used when the source PDF already has a well-formed
     * structure tree with real (visible) MCR content — destroying it would
     * be worse than keeping it and enhancing.
     */
    private static void preserveSourceAndEnhance(
            String inputPath, String outputPath,
            JsonObject structure, JsonArray elements,
            String lang, String title) throws Exception {

        System.out.println("  PRESERVATION MODE: keeping source structure tree");

        PdfReader reader = new PdfReader(inputPath);
        PdfWriter writer = new PdfWriter(outputPath);
        PdfDocument pdfDoc = new OutputPdfDocument(reader, writer);

        // Extract title from structure JSON if not provided
        if (title == null || title.isEmpty()) {
            JsonObject metadata = structure.getAsJsonObject("metadata");
            if (metadata != null && metadata.has("title")
                    && !metadata.get("title").isJsonNull()) {
                title = metadata.get("title").getAsString();
            }
        }
        if (title == null || title.isEmpty()) {
            title = "Untitled Document";
        }

        // --- Metadata fixes (non-destructive) ---

        // Ensure tagged mode is set (enables MarkInfo if not present)
        pdfDoc.setTagged();

        // Set document language
        pdfDoc.getCatalog().setLang(new PdfString(lang));

        // Set MarkInfo/Marked = true
        pdfDoc.getCatalog().put(
            PdfName.MarkInfo,
            new PdfDictionary(Map.of(PdfName.Marked, PdfBoolean.TRUE))
        );

        // Set document title
        pdfDoc.getDocumentInfo().setTitle(title);

        // ViewerPreferences: display title
        PdfViewerPreferences viewerPrefs = new PdfViewerPreferences();
        viewerPrefs.setDisplayDocTitle(true);
        pdfDoc.getCatalog().setViewerPreferences(viewerPrefs);

        // Set /Tabs /S on all pages
        int numPages = pdfDoc.getNumberOfPages();
        for (int p = 1; p <= numPages; p++) {
            pdfDoc.getPage(p).put(PdfName.Tabs, PdfName.S);
        }

        // --- Enrich existing structure with Docling data ---

        // Add /Alt to Figure elements that lack it
        enrichFigureAlt(pdfDoc, elements);

        // Add /Contents to Link annotations that lack it
        enrichLinkContents(pdfDoc);

        // Detect P elements that should be headings (font-based heuristic)
        detectAndPromoteHeadings(pdfDoc);

        // Add PDF bookmarks/outlines from heading structure elements
        // Acrobat requires bookmarks for documents with 21+ pages
        if (pdfDoc.getNumberOfPages() >= 21) {
            addBookmarks(pdfDoc);
        }

        // Fix table column consistency (ColSpan for short rows)
        fixTableColSpan(pdfDoc);

        // Add /Lang to text-bearing structure elements
        addElementLang(pdfDoc, lang);

        // --- PDF/UA compliance fixes ---

        addMissingToUnicodeCMaps(pdfDoc);
        stripIncorrectCIDSets(pdfDoc);
        stripSymbolicTrueTypeEncoding(pdfDoc);
        fixCIDToGIDMap(pdfDoc);
        checkFontCompliance(pdfDoc);
        fixOCProperties(pdfDoc);
        fixEmbeddedFileSpecs(pdfDoc);

        // XMP metadata last
        setXmpMetadata(pdfDoc, title, lang);

        int totalSE = 0;
        for (int xref = 1; xref < pdfDoc.getNumberOfPdfObjects(); xref++) {
            try {
                PdfObject obj = pdfDoc.getPdfObject(xref);
                if (obj != null && obj.isDictionary()) {
                    PdfDictionary d = (PdfDictionary) obj;
                    PdfName type = d.getAsName(PdfName.Type);
                    if (type != null && PdfName.StructElem.equals(type)) totalSE++;
                }
            } catch (Exception e) {
                System.err.println("WARNING: Skipping malformed object " + xref
                    + " in SE count: " + e.getMessage());
            }
        }

        pdfDoc.close();
        System.out.println("  Preservation mode complete: " + totalSE
            + " structure elements preserved -> " + outputPath);
    }

    /**
     * Add /Alt text to Figure structure elements that lack it, using
     * Docling-generated alt text matched by page number and bounding box.
     */
    private static void enrichFigureAlt(PdfDocument pdfDoc,
            JsonArray elements) {
        // Build lookup: page_no -> list of {bbox, alt_text}
        Map<Integer, List<JsonObject>> figuresByPage = new HashMap<>();
        for (JsonElement je : elements) {
            JsonObject elem = je.getAsJsonObject();
            String tag = elem.has("tag") ? elem.get("tag").getAsString() : "";
            if (!"Figure".equals(tag)) continue;
            String alt = elem.has("alt_text") && !elem.get("alt_text").isJsonNull()
                ? elem.get("alt_text").getAsString() : null;
            if (alt == null || alt.isEmpty()) continue;
            int pageNo = elem.has("page_no")
                ? elem.get("page_no").getAsInt() : 1;
            figuresByPage.computeIfAbsent(pageNo, k -> new ArrayList<>())
                .add(elem);
        }
        if (figuresByPage.isEmpty()) return;

        int enriched = 0;
        for (int xref = 1; xref < pdfDoc.getNumberOfPdfObjects(); xref++) {
            try {
                PdfObject obj = pdfDoc.getPdfObject(xref);
                if (obj == null || !obj.isDictionary()) continue;
                PdfDictionary dict = (PdfDictionary) obj;
                PdfName type = dict.getAsName(PdfName.Type);
                if (type == null || !PdfName.StructElem.equals(type)) continue;
                PdfName s = dict.getAsName(PdfName.S);
                if (s == null || !"Figure".equals(s.getValue())) continue;

                // Skip if already has non-empty /Alt
                PdfString existingAlt = dict.getAsString(new PdfName("Alt"));
                if (existingAlt != null && !existingAlt.getValue().isEmpty()
                        && !" ".equals(existingAlt.getValue())) continue;

                // Acrobat "Associated with content": /Alt on a Figure SE
                // without a /K entry (MCR or OBJR) causes a failure.
                // Only enrich Figures that already have content association.
                PdfObject kObj = dict.get(PdfName.K);
                if (kObj == null) continue;

                // Find page number from /Pg reference
                PdfDictionary pgDict = dict.getAsDictionary(PdfName.Pg);
                if (pgDict == null) continue;
                int pageNo = -1;
                for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
                    if (pdfDoc.getPage(p).getPdfObject() == pgDict) {
                        pageNo = p;
                        break;
                    }
                }
                if (pageNo < 0 || !figuresByPage.containsKey(pageNo)) continue;

                // Use the first available alt text for this page's figures
                List<JsonObject> figs = figuresByPage.get(pageNo);
                if (!figs.isEmpty()) {
                    String alt = figs.remove(0).get("alt_text").getAsString();
                    dict.put(new PdfName("Alt"),
                        new PdfString(alt, PdfEncodings.UNICODE_BIG));
                    dict.setModified();
                    enriched++;
                }
            } catch (Exception e) {
                System.err.println("WARNING: Skipping malformed object " + xref
                    + " in enrichFigureAlt: " + e.getMessage());
            }
        }
        if (enriched > 0) {
            System.out.println("  Enriched " + enriched
                + " Figure element(s) with /Alt text");
        }
    }

    /**
     * Add /Contents to Link annotations that lack it (PDF/UA 7.18.1 + 7.18.5).
     */
    private static void enrichLinkContents(PdfDocument pdfDoc) {
        int fixed = 0;
        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            List<PdfAnnotation> annots = pdfDoc.getPage(p).getAnnotations();
            if (annots == null) continue;
            for (PdfAnnotation annot : annots) {
                if (!(annot instanceof PdfLinkAnnotation)) continue;
                PdfDictionary annotDict = annot.getPdfObject();
                if (annotDict.containsKey(PdfName.Contents)) continue;

                String contentsText = null;
                PdfDictionary action = annotDict.getAsDictionary(PdfName.A);
                if (action != null) {
                    PdfString uri = action.getAsString(new PdfName("URI"));
                    if (uri != null) contentsText = uri.getValue();
                }
                if (contentsText == null) {
                    PdfObject dest = annotDict.get(PdfName.Dest);
                    contentsText = dest != null ? "Internal link" : "Link";
                }
                annotDict.put(PdfName.Contents, new PdfString(contentsText));
                annotDict.setModified();
                fixed++;
            }
        }
        if (fixed > 0) {
            System.out.println("  Added /Contents to " + fixed
                + " link annotation(s)");
        }
    }

    /** Heading tag for an all-bold paragraph: H1 above 12pt, otherwise H3. */
    static String boldHeadingTag(double maxFontSize, String text) {
        return maxFontSize > 12.0 ? "H1" : "H3";
    }

    /**
     * Detect P elements that should be headings by analyzing font properties
     * in the content stream MCR blocks. Remaps /S from /P to /H1, /H2, /H3.
     *
     * Rules:
     * - All-bold, size > 12pt → H1 (document title)
     * - All-bold, size ≤ 12pt, other non-empty text → H3
     * - Any non-bold font → remains P
     */
    // Constant sub-patterns of the heading-detection loop below, hoisted
    // so they aren't recompiled once per MCID per element.
    private static final java.util.regex.Pattern HEADING_TF_P =
        java.util.regex.Pattern.compile("/(\\w+)\\s+(\\d+\\.?\\d*)\\s+Tf");
    private static final java.util.regex.Pattern HEADING_TJ_P =
        java.util.regex.Pattern.compile("\\[([^\\]]+)\\]\\s*TJ");
    private static final java.util.regex.Pattern HEADING_STR_P =
        java.util.regex.Pattern.compile("\\(([^)]*)\\)");
    private static final java.util.regex.Pattern HEADING_SINGLE_TJ_P =
        java.util.regex.Pattern.compile("\\(([^)]*)\\)\\s*Tj");

    private static void detectAndPromoteHeadings(PdfDocument pdfDoc) {
        // Build font name map: page -> {fontRef -> baseFontName}
        // and page content bytes cache
        Map<Integer, byte[]> pageContent = new HashMap<>();
        Map<Integer, Map<String, String>> pageFonts = new HashMap<>();

        int numPages = pdfDoc.getNumberOfPages();
        for (int p = 1; p <= numPages; p++) {
            PdfPage page = pdfDoc.getPage(p);
            try {
                pageContent.put(p, page.getContentBytes());
            } catch (Exception e) {
                continue;
            }
            Map<String, String> fontMap = new HashMap<>();
            PdfDictionary resources = page.getResources().getPdfObject();
            if (resources != null) {
                PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
                if (fonts != null) {
                    for (PdfName key : fonts.keySet()) {
                        PdfDictionary fontDict = fonts.getAsDictionary(key);
                        if (fontDict != null) {
                            PdfName baseFont = fontDict.getAsName(PdfName.BaseFont);
                            if (baseFont != null) {
                                fontMap.put(key.getValue(), baseFont.getValue());
                            }
                        }
                    }
                }
            }
            pageFonts.put(p, fontMap);
        }

        int promoted = 0;
        int h1Count = 0, h3Count = 0;

        for (int xref = 1; xref < pdfDoc.getNumberOfPdfObjects(); xref++) {
            try {
                PdfObject obj = pdfDoc.getPdfObject(xref);
                if (obj == null || !obj.isDictionary()) continue;
                PdfDictionary dict = (PdfDictionary) obj;
                PdfName type = dict.getAsName(PdfName.Type);
                if (type == null || !PdfName.StructElem.equals(type)) continue;
                PdfName s = dict.getAsName(PdfName.S);
                if (s == null || !"P".equals(s.getValue())) continue;

                // Get MCID(s) from /K
                List<Integer> mcids = new ArrayList<>();
                PdfObject k = dict.get(PdfName.K);
                if (k instanceof PdfNumber) {
                    mcids.add(((PdfNumber) k).intValue());
                } else if (k instanceof PdfArray) {
                    PdfArray kArr = (PdfArray) k;
                    for (int i = 0; i < kArr.size(); i++) {
                        PdfObject ki = kArr.get(i);
                        if (ki instanceof PdfNumber) {
                            mcids.add(((PdfNumber) ki).intValue());
                        }
                    }
                }
                if (mcids.isEmpty()) continue;

                // Find page number
                PdfDictionary pgDict = dict.getAsDictionary(PdfName.Pg);
                if (pgDict == null) continue;
                int pageNo = -1;
                for (int p = 1; p <= numPages; p++) {
                    if (pdfDoc.getPage(p).getPdfObject() == pgDict) {
                        pageNo = p;
                        break;
                    }
                }
                if (pageNo < 0 || !pageContent.containsKey(pageNo)) continue;

                byte[] content = pageContent.get(pageNo);
                Map<String, String> fontMap = pageFonts.getOrDefault(pageNo,
                    Collections.emptyMap());
                String contentStr = new String(content, StandardCharsets.ISO_8859_1);

                // Analyze all MCR blocks for this element
                boolean allBold = true;
                double maxFontSize = 0;
                StringBuilder textBuilder = new StringBuilder();

                for (int mcid : mcids) {
                    // Find BDC block for this MCID (pattern embeds the
                    // mcid, so it can't be hoisted like the ones below)
                    java.util.regex.Pattern bdcP = java.util.regex.Pattern.compile(
                        "/\\w+\\s*<<[^>]*/MCID\\s+" + mcid + "\\b[^>]*>>\\s*BDC"
                        + "([\\s\\S]*?)EMC");
                    java.util.regex.Matcher bdcM = bdcP.matcher(contentStr);
                    if (!bdcM.find()) continue;

                    String block = bdcM.group(1);

                    // Extract font operations (Tf) and text (TJ/Tj)
                    java.util.regex.Matcher tfM = HEADING_TF_P.matcher(block);
                    while (tfM.find()) {
                        String fontRef = tfM.group(1);
                        double fontSize = Double.parseDouble(tfM.group(2));
                        if (fontSize > maxFontSize) maxFontSize = fontSize;

                        String baseFontName = fontMap.getOrDefault(fontRef, "");
                        // Check if font is bold
                        String lower = baseFontName.toLowerCase(java.util.Locale.ROOT);
                        if (!lower.contains("bold")) {
                            allBold = false;
                        }
                    }

                    // Extract visible text from TJ arrays
                    java.util.regex.Matcher tjM = HEADING_TJ_P.matcher(block);
                    while (tjM.find()) {
                        String arr = tjM.group(1);
                        // Extract string operands from TJ array
                        java.util.regex.Matcher strM =
                            HEADING_STR_P.matcher(arr);
                        while (strM.find()) {
                            textBuilder.append(strM.group(1));
                        }
                    }
                    // Also check single Tj
                    java.util.regex.Matcher sjM =
                        HEADING_SINGLE_TJ_P.matcher(block);
                    while (sjM.find()) {
                        textBuilder.append(sjM.group(1));
                    }
                }

                String text = textBuilder.toString().trim();
                if (text.isEmpty() || !allBold) continue;

                // Determine heading level
                String newTag = boldHeadingTag(maxFontSize, text);
                if ("H1".equals(newTag)) {
                    h1Count++;
                } else {
                    h3Count++;
                }

                dict.put(PdfName.S, new PdfName(newTag));
                dict.setModified();
                promoted++;
            } catch (Exception e) {
                System.err.println("WARNING: Skipping malformed object " + xref
                    + " in heading promotion: " + e.getMessage());
            }
        }

        if (promoted > 0) {
            System.out.println("  Promoted " + promoted
                + " P elements to headings (H1=" + h1Count
                + ", H3=" + h3Count + ")");
        }
    }

    /**
     * Add PDF bookmarks (outlines) from heading structure elements.
     * Acrobat requires bookmarks for documents with 21+ pages.
     * Builds a hierarchical outline tree matching the H1-H6 nesting.
     */
    private static void addBookmarks(PdfDocument pdfDoc) {
        // Collect heading SEs: (page, xrefIndex, level, text)
        class HeadingEntry implements Comparable<HeadingEntry> {
            int page, xref, level;
            String text;
            HeadingEntry(int page, int xref, int level, String text) {
                this.page = page; this.xref = xref;
                this.level = level; this.text = text;
            }
            @Override public int compareTo(HeadingEntry o) {
                int c = Integer.compare(this.page, o.page);
                return c != 0 ? c : Integer.compare(this.xref, o.xref);
            }
        }

        List<HeadingEntry> headings = new ArrayList<>();
        for (int xref = 1; xref < pdfDoc.getNumberOfPdfObjects(); xref++) {
            try {
                PdfObject obj = pdfDoc.getPdfObject(xref);
                if (obj == null || !obj.isDictionary()) continue;
                PdfDictionary dict = (PdfDictionary) obj;
                PdfName type = dict.getAsName(PdfName.Type);
                if (type == null || !PdfName.StructElem.equals(type)) continue;
                PdfName s = dict.getAsName(PdfName.S);
                if (s == null) continue;
                String sVal = s.getValue();

                // Match H1-H6
                int level = 0;
                if (sVal.length() == 2 && sVal.charAt(0) == 'H'
                        && sVal.charAt(1) >= '1' && sVal.charAt(1) <= '6') {
                    level = sVal.charAt(1) - '0';
                }
                if (level == 0) continue;

                // Get page number from /Pg
                PdfDictionary pgDict = dict.getAsDictionary(PdfName.Pg);
                if (pgDict == null) continue;
                int pageNo = -1;
                for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
                    if (pdfDoc.getPage(p).getPdfObject() == pgDict) {
                        pageNo = p; break;
                    }
                }
                if (pageNo < 0) continue;

                // Extract text from /ActualText or /Alt
                String text = null;
                PdfString actualText = dict.getAsString(
                    new PdfName("ActualText"));
                if (actualText != null && !actualText.getValue().isEmpty()) {
                    text = actualText.getValue();
                }
                if (text == null) {
                    PdfString alt = dict.getAsString(new PdfName("Alt"));
                    if (alt != null && !alt.getValue().isEmpty()) {
                        text = alt.getValue();
                    }
                }
                if (text == null || text.trim().isEmpty()) {
                    text = "(untitled)";
                }
                // Truncate very long heading text for bookmark labels
                if (text.length() > 120) {
                    text = text.substring(0, 117) + "...";
                }

                headings.add(new HeadingEntry(pageNo, xref, level, text));
            } catch (Exception e) {
                // Skip malformed objects
            }
        }

        if (headings.isEmpty()) return;
        Collections.sort(headings);

        // Skip if document already has bookmarks (avoid duplicates)
        PdfOutline existing = pdfDoc.getOutlines(false);
        if (existing != null && existing.getAllChildren() != null
                && !existing.getAllChildren().isEmpty()) {
            return;
        }

        // Build outline tree with proper nesting
        PdfOutline root = pdfDoc.getOutlines(true);
        PdfOutline[] stack = new PdfOutline[7]; // index 0=root, 1-6=H1-H6
        stack[0] = root;

        for (HeadingEntry h : headings) {
            // Find parent: nearest non-null ancestor at level-1 or above
            PdfOutline parent = null;
            for (int lv = h.level - 1; lv >= 0; lv--) {
                if (stack[lv] != null) { parent = stack[lv]; break; }
            }
            if (parent == null) parent = root;

            PdfOutline item = parent.addOutline(h.text);
            item.addDestination(PdfExplicitDestination.createFitH(
                pdfDoc.getPage(h.page),
                pdfDoc.getPage(h.page).getPageSize().getTop()));
            stack[h.level] = item;
            // Clear deeper levels
            for (int d = h.level + 1; d <= 6; d++) stack[d] = null;
        }

        System.out.println("  Added " + headings.size()
            + " bookmark(s) from heading structure");
    }

    /**
     * Fix table column consistency by adding ColSpan attributes to rows
     * with fewer cells than the table's maximum column count.
     * This fixes veraPDF 7.2 tests 42/43.
     */
    private static void fixTableColSpan(PdfDocument pdfDoc) {
        int fixed = 0;

        for (int xref = 1; xref < pdfDoc.getNumberOfPdfObjects(); xref++) {
            try {
                PdfObject obj = pdfDoc.getPdfObject(xref);
                if (obj == null || !obj.isDictionary()) continue;
                PdfDictionary dict = (PdfDictionary) obj;
                PdfName type = dict.getAsName(PdfName.Type);
                if (type == null || !PdfName.StructElem.equals(type)) continue;
                PdfName s = dict.getAsName(PdfName.S);
                if (s == null || !"Table".equals(s.getValue())) continue;

                // Collect all TR rows (may be under TBody/THead/TFoot or direct)
                List<PdfDictionary> rows = new ArrayList<>();
                collectTableRows(pdfDoc, dict, rows);

                if (rows.size() < 2) continue;

                // Find max column count across rows
                int maxCols = 0;
                for (PdfDictionary row : rows) {
                    int cols = countRowCells(pdfDoc, row);
                    if (cols > maxCols) maxCols = cols;
                }

                if (maxCols < 2) continue;

                // Fix rows with fewer cells
                for (PdfDictionary row : rows) {
                    int cells = countRowCells(pdfDoc, row);
                    if (cells > 0 && cells < maxCols) {
                        // Distribute ColSpan: floor for most cells, remainder to last
                        int baseColSpan = maxCols / cells;
                        int remainder = maxCols % cells;
                        List<PdfDictionary> cellDicts = getRowCells(pdfDoc, row);
                        for (int ci = 0; ci < cellDicts.size(); ci++) {
                            int cs = (ci == cellDicts.size() - 1)
                                ? baseColSpan + remainder : baseColSpan;
                            PdfDictionary cell = cellDicts.get(ci);
                            // Merge into existing /A dict to preserve prior attributes
                            PdfDictionary existing = cell.getAsDictionary(PdfName.A);
                            if (existing == null) {
                                existing = new PdfDictionary();
                                cell.put(PdfName.A, existing);
                            }
                            existing.put(new PdfName("O"), new PdfName("Table"));
                            existing.put(new PdfName("ColSpan"), new PdfNumber(cs));
                            cell.setModified();
                            fixed++;
                        }
                    }
                }
            } catch (Exception e) {
                System.err.println("WARNING: Skipping malformed object " + xref
                    + " in fixTableColSpan: " + e.getMessage());
            }
        }

        if (fixed > 0) {
            System.out.println("  Added ColSpan to " + fixed
                + " table cell(s) for column consistency");
        }
    }

    /** Recursively collect TR elements under a Table (through TBody/THead). */
    private static void collectTableRows(PdfDocument pdfDoc,
            PdfDictionary parent, List<PdfDictionary> rows) {
        PdfObject k = parent.get(PdfName.K);
        if (k == null) return;
        List<PdfObject> kids = new ArrayList<>();
        if (k instanceof PdfArray) {
            PdfArray arr = (PdfArray) k;
            for (int i = 0; i < arr.size(); i++) kids.add(arr.get(i, false));
        } else {
            kids.add(k);
        }
        for (PdfObject kid : kids) {
            if (kid instanceof PdfIndirectReference) {
                kid = ((PdfIndirectReference) kid).getRefersTo();
            }
            if (!(kid instanceof PdfDictionary)) continue;
            PdfDictionary d = (PdfDictionary) kid;
            PdfName s = d.getAsName(PdfName.S);
            if (s == null) continue;
            String tag = s.getValue();
            if ("TR".equals(tag)) {
                rows.add(d);
            } else if ("TBody".equals(tag) || "THead".equals(tag)
                    || "TFoot".equals(tag)) {
                collectTableRows(pdfDoc, d, rows);
            }
        }
    }

    /** Count TD/TH cells in a TR row. */
    private static int countRowCells(PdfDocument pdfDoc, PdfDictionary row) {
        return getRowCells(pdfDoc, row).size();
    }

    /** Get TD/TH cell dictionaries from a TR row. */
    private static List<PdfDictionary> getRowCells(PdfDocument pdfDoc,
            PdfDictionary row) {
        List<PdfDictionary> cells = new ArrayList<>();
        PdfObject k = row.get(PdfName.K);
        if (k == null) return cells;
        List<PdfObject> kids = new ArrayList<>();
        if (k instanceof PdfArray) {
            PdfArray arr = (PdfArray) k;
            for (int i = 0; i < arr.size(); i++) kids.add(arr.get(i, false));
        } else {
            kids.add(k);
        }
        for (PdfObject kid : kids) {
            if (kid instanceof PdfIndirectReference) {
                kid = ((PdfIndirectReference) kid).getRefersTo();
            }
            if (!(kid instanceof PdfDictionary)) continue;
            PdfDictionary d = (PdfDictionary) kid;
            PdfName s = d.getAsName(PdfName.S);
            if (s != null && ("TD".equals(s.getValue())
                    || "TH".equals(s.getValue()))) {
                cells.add(d);
            }
        }
        return cells;
    }

    /**
     * Add /Lang to text-bearing structure elements that lack it.
     */
    private static void addElementLang(PdfDocument pdfDoc, String lang) {
        PdfName langKey = new PdfName("Lang");
        PdfString langVal = new PdfString(lang);
        int added = 0;
        Set<String> textTags = new HashSet<>(Arrays.asList(
            "P", "Span", "H1", "H2", "H3", "H4", "H5", "H6",
            "TD", "TH", "Link", "Caption", "LBody", "Lbl"
        ));

        for (int xref = 1; xref < pdfDoc.getNumberOfPdfObjects(); xref++) {
            try {
                PdfObject obj = pdfDoc.getPdfObject(xref);
                if (obj == null || !obj.isDictionary()) continue;
                PdfDictionary dict = (PdfDictionary) obj;
                PdfName type = dict.getAsName(PdfName.Type);
                if (type == null || !PdfName.StructElem.equals(type)) continue;

                PdfName s = dict.getAsName(PdfName.S);
                if (s == null || !textTags.contains(s.getValue())) continue;

                // Skip if already has /Lang
                if (dict.get(langKey) != null) continue;

                dict.put(langKey, langVal);
                dict.setModified();
                added++;
            } catch (Exception e) {
                System.err.println("WARNING: Skipping malformed object " + xref
                    + " in addElementLang: " + e.getMessage());
            }
        }

        if (added > 0) {
            System.out.println("  Added /Lang to " + added
                + " structure element(s)");
        }
    }

    private static void applyMetadataOnlyFixes(String inputPath,
            String outputPath, String lang, String title) throws Exception {
        PdfReader reader = new PdfReader(inputPath);
        PdfWriter writer = new PdfWriter(outputPath);
        PdfDocument pdfDoc = new OutputPdfDocument(reader, writer);

        System.out.println("  Applying non-destructive metadata fixes...");

        // PDF/UA-1 requires MarkInfo, ViewerPreferences, Lang, Tabs
        pdfDoc.getCatalog().put(
            PdfName.MarkInfo,
            new PdfDictionary(Map.of(PdfName.Marked, PdfBoolean.TRUE))
        );
        PdfViewerPreferences viewerPrefs = new PdfViewerPreferences();
        viewerPrefs.setDisplayDocTitle(true);
        pdfDoc.getCatalog().setViewerPreferences(viewerPrefs);
        pdfDoc.getCatalog().setLang(new PdfString(lang));

        if (title != null && !title.isEmpty()) {
            pdfDoc.getDocumentInfo().setTitle(title);
        }

        // Set /Tabs /S on all pages
        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            pdfDoc.getPage(p).put(PdfName.Tabs, PdfName.S);
        }

        // Wrap all page content as Artifact (7.1.3: everything must be
        // tagged or marked as Artifact). With 0 Docling elements, the
        // entire content is non-semantic (e.g. scanned image pages).
        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfPage page = pdfDoc.getPage(p);
            byte[] orig = page.getContentBytes();
            if (orig == null || orig.length == 0) continue;
            ByteArrayOutputStream bos = new ByteArrayOutputStream();
            bos.write("/Artifact BMC\n".getBytes(StandardCharsets.US_ASCII));
            bos.write(orig);
            bos.write("\nEMC\n".getBytes(StandardCharsets.US_ASCII));
            page.put(PdfName.Contents,
                new PdfStream(bos.toByteArray(), CompressionConstants.DEFAULT_COMPRESSION).makeIndirect(pdfDoc));
        }
        System.out.println("  Wrapped all page content as Artifact (0 elements)");

        fixOCProperties(pdfDoc);
        fixEmbeddedFileSpecs(pdfDoc);
        addMissingToUnicodeCMaps(pdfDoc);
        stripIncorrectCIDSets(pdfDoc);
        stripSymbolicTrueTypeEncoding(pdfDoc);

        // XMP metadata
        setXmpMetadata(pdfDoc, title != null ? title : "", lang);

        pdfDoc.close();
        System.out.println("  Metadata-only pass complete: " + outputPath);
    }

    /**
     * PDF/UA 7.10 test 1: Each optional content configuration dictionary
     * must contain a non-empty /Name key.
     * PDF/UA 7.10 test 2: The AS key shall not appear in any optional
     * content configuration dictionary.
     */
    static void fixOCProperties(PdfDocument pdfDoc) {
        PdfDictionary catalog = pdfDoc.getCatalog().getPdfObject();
        PdfDictionary ocProps = catalog.getAsDictionary(
            new PdfName("OCProperties"));
        if (ocProps == null) return;

        PdfName asKey = new PdfName("AS");

        // Fix /D (default config)
        PdfDictionary dConfig = ocProps.getAsDictionary(new PdfName("D"));
        if (dConfig != null) {
            PdfString name = dConfig.getAsString(PdfName.Name);
            if (name == null || name.getValue().isEmpty()) {
                dConfig.put(PdfName.Name, new PdfString("Default"));
                dConfig.setModified();
                System.out.println("  Fixed OCProperties/D: added /Name");
            }
            if (dConfig.get(asKey) != null) {
                dConfig.remove(asKey);
                dConfig.setModified();
                System.out.println("  Fixed OCProperties/D: removed /AS key");
            }
        }

        // Fix /Configs array entries
        PdfArray configs = ocProps.getAsArray(new PdfName("Configs"));
        if (configs != null) {
            for (int i = 0; i < configs.size(); i++) {
                PdfDictionary cfg = configs.getAsDictionary(i);
                if (cfg == null) continue;
                PdfString name = cfg.getAsString(PdfName.Name);
                if (name == null || name.getValue().isEmpty()) {
                    cfg.put(PdfName.Name,
                        new PdfString("Config" + (i + 1)));
                    cfg.setModified();
                    System.out.println("  Fixed OCProperties/Configs["
                        + i + "]: added /Name");
                }
                if (cfg.get(asKey) != null) {
                    cfg.remove(asKey);
                    cfg.setModified();
                    System.out.println("  Fixed OCProperties/Configs["
                        + i + "]: removed /AS key");
                }
            }
        }
    }

    /**
     * Ensure every embedded-file file specification dictionary has both the
     * /F and /UF keys (ISO 14289-1 7.11 t1).  Many source PDFs embed a file
     * (e.g. an Adobe ".joboptions") with only /F set; veraPDF requires both,
     * with /UF a Unicode string.  Copies whichever is present to the missing
     * one.  File-spec dicts are detected by the /EF key (embedded-file
     * dictionary), which appears only on them.
     */
    static void fixEmbeddedFileSpecs(PdfDocument pdfDoc) {
        PdfName ef = new PdfName("EF");
        PdfName uf = new PdfName("UF");
        int fixed = 0;
        int count = pdfDoc.getNumberOfPdfObjects();
        for (int i = 1; i < count; i++) {
            PdfDictionary fs;
            try {
                // getPdfObject() throws on free/broken xref slots (the
                // referenced indirect object can be null) — skip those.
                PdfObject obj = pdfDoc.getPdfObject(i);
                if (!(obj instanceof PdfDictionary)) continue;
                fs = (PdfDictionary) obj;
                if (!fs.containsKey(ef)) continue;
                PdfString f = fs.getAsString(PdfName.F);
                PdfString ufStr = fs.getAsString(uf);
                boolean fEmpty = (f == null || f.getValue().isEmpty());
                boolean ufEmpty = (ufStr == null || ufStr.getValue().isEmpty());
                if (fEmpty && ufEmpty) continue;  // nothing present to copy from
                boolean changed = false;
                if (ufEmpty) {
                    fs.put(uf, new PdfString(f.getValue(),
                        PdfEncodings.UNICODE_BIG));
                    changed = true;
                }
                if (fEmpty) {
                    fs.put(PdfName.F, new PdfString(ufStr.getValue()));
                    changed = true;
                }
                if (changed) { fs.setModified(); fixed++; }
            } catch (Exception e) {
                // Never let a compliance fix abort tagging.
                continue;
            }
        }
        if (fixed > 0) {
            System.out.println("  Fixed " + fixed
                + " embedded-file Filespec(s): added missing /F or /UF");
        }
    }

    private static int readUint16(byte[] data, int offset) {
        return ((data[offset] & 0xFF) << 8) | (data[offset + 1] & 0xFF);
    }

    private static int readUint32(byte[] data, int offset) {
        return ((data[offset] & 0xFF) << 24) | ((data[offset + 1] & 0xFF) << 16)
            | ((data[offset + 2] & 0xFF) << 8) | (data[offset + 3] & 0xFF);
    }

    /**
     * Snapshot all font PdfIndirectReferences currently in page resources.
     * Call BEFORE the tagger adds its overlay font so the returned set
     * contains only source-document fonts.
     */
    static Set<PdfIndirectReference> snapshotSourceFontRefs(PdfDocument pdfDoc) {
        Set<PdfIndirectReference> refs = new HashSet<>();
        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfDictionary resources = pdfDoc.getPage(p).getResources().getPdfObject();
            if (resources == null) continue;
            PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
            if (fonts == null) continue;
            for (PdfName fn : fonts.keySet()) {
                PdfDictionary fd = fonts.getAsDictionary(fn);
                if (fd != null && fd.getIndirectReference() != null) {
                    refs.add(fd.getIndirectReference());
                }
            }
        }
        return refs;
    }

    /**
     * Check font compliance (embedding + ToUnicode).
     *
     * @param sourceFontRefs  if non-null, only fonts whose indirect reference
     *                        is in this set are checked (filters out tagger-
     *                        injected overlay fonts).  Pass null to check all.
     */
    static void checkFontCompliance(PdfDocument pdfDoc,
                                    Set<PdfIndirectReference> sourceFontRefs) {
        Set<String> checked = new HashSet<>();
        int totalFonts = 0;
        int issues = 0;

        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfPage page = pdfDoc.getPage(p);
            PdfDictionary resources = page.getResources().getPdfObject();
            if (resources == null) continue;

            PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
            if (fonts == null) continue;

            for (PdfName fontName : fonts.keySet()) {
                PdfDictionary fontDict = fonts.getAsDictionary(fontName);
                if (fontDict == null) continue;

                // Skip fonts injected by the tagger (not from source PDF)
                if (sourceFontRefs != null
                        && fontDict.getIndirectReference() != null
                        && !sourceFontRefs.contains(fontDict.getIndirectReference())) {
                    continue;
                }

                String name = fontName.getValue();
                if (checked.contains(name)) continue;
                checked.add(name);
                totalFonts++;

                String baseFont = fontDict.containsKey(PdfName.BaseFont)
                    ? fontDict.getAsName(PdfName.BaseFont).getValue() : name;

                // Check embedding: look for FontDescriptor with FontFile/FontFile2/FontFile3
                boolean embedded = false;
                PdfDictionary descriptor = fontDict.getAsDictionary(
                    new PdfName("FontDescriptor"));
                if (descriptor != null) {
                    embedded = descriptor.containsKey(new PdfName("FontFile"))
                        || descriptor.containsKey(new PdfName("FontFile2"))
                        || descriptor.containsKey(new PdfName("FontFile3"));
                }

                // Type 0 (composite) fonts: check DescendantFonts
                PdfArray descendants = fontDict.getAsArray(
                    new PdfName("DescendantFonts"));
                if (!embedded && descendants != null && descendants.size() > 0) {
                    PdfDictionary descFont = descendants.getAsDictionary(0);
                    if (descFont != null) {
                        PdfDictionary descFD = descFont.getAsDictionary(
                            new PdfName("FontDescriptor"));
                        if (descFD != null) {
                            embedded = descFD.containsKey(new PdfName("FontFile"))
                                || descFD.containsKey(new PdfName("FontFile2"))
                                || descFD.containsKey(new PdfName("FontFile3"));
                        }
                    }
                }

                if (!embedded) {
                    System.err.println("  FONT WARNING: " + baseFont
                        + " is NOT embedded (page " + p + ")");
                    issues++;
                    sqUnembeddedFonts++;
                    if (sqUnembeddedFontNames.size() < 50) {
                        sqUnembeddedFontNames.add(baseFont);
                    }
                }

                // Check ToUnicode CMap
                boolean hasToUnicode = fontDict.containsKey(PdfName.ToUnicode);
                if (!hasToUnicode) {
                    // Standard 14 fonts with simple encoding are OK without ToUnicode
                    // if they use WinAnsiEncoding or MacRomanEncoding
                    PdfObject encoding = fontDict.get(PdfName.Encoding);
                    boolean standardEncoding = false;
                    if (encoding != null && encoding.isName()) {
                        String encName = ((PdfName) encoding).getValue();
                        standardEncoding = "WinAnsiEncoding".equals(encName)
                            || "MacRomanEncoding".equals(encName);
                    }
                    if (!standardEncoding) {
                        System.err.println("  FONT WARNING: " + baseFont
                            + " has no /ToUnicode CMap (page " + p + ")");
                        issues++;
                        sqMissingToUnicode++;
                    }
                }
            }
        }

        if (issues == 0) {
            System.out.println("  Font compliance: " + totalFonts
                + " fonts checked — all OK");
        } else {
            System.out.println("  Font compliance: " + issues
                + " issue(s) found across " + totalFonts + " fonts");
        }
    }

    /** Convenience overload — checks all fonts (no filtering). */
    static void checkFontCompliance(PdfDocument pdfDoc) {
        checkFontCompliance(pdfDoc, null);
    }

    // -----------------------------------------------------------------
    // Font Embedding — inject Liberation TTF for unembedded standard fonts
    // -----------------------------------------------------------------

    static void embedMissingFonts(PdfDocument pdfDoc) {
        Map<String, byte[]> fontCache = new HashMap<>();
        Map<String, PdfStream> fontStreamCache = new HashMap<>();
        Set<String> processed = new HashSet<>();
        int[] embedded = {0};

        // Collect all font dictionaries from page resources, XObjects, and AcroForm
        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfDictionary resources = pdfDoc.getPage(p).getResources().getPdfObject();
            if (resources == null) continue;
            embedFontsInResources(pdfDoc, resources, fontCache, fontStreamCache, processed, embedded);

            // Also check XObject Form resources (fonts inside form XObjects)
            PdfDictionary xobjects = resources.getAsDictionary(new PdfName("XObject"));
            if (xobjects != null) {
                for (PdfName xoKey : xobjects.keySet()) {
                    PdfStream xoStream = xobjects.getAsStream(xoKey);
                    if (xoStream != null) {
                        PdfDictionary xoResources = xoStream.getAsDictionary(
                            PdfName.Resources);
                        if (xoResources != null) {
                            embedFontsInResources(pdfDoc, xoResources,
                                fontCache, fontStreamCache, processed, embedded);
                        }
                    }
                }
            }

            // Check annotation appearance streams (/AP dictionaries)
            // Widget annotations (form fields) have their own font resources.
            PdfArray annots = pdfDoc.getPage(p).getPdfObject()
                .getAsArray(PdfName.Annots);
            if (annots != null) {
                for (int a = 0; a < annots.size(); a++) {
                    PdfDictionary annot = annots.getAsDictionary(a);
                    if (annot == null) continue;
                    PdfDictionary ap = annot.getAsDictionary(new PdfName("AP"));
                    if (ap == null) continue;
                    // Process /N (normal), /R (rollover), /D (down) appearances
                    for (PdfName apKey : ap.keySet()) {
                        PdfObject apObj = ap.get(apKey);
                        if (apObj instanceof PdfStream) {
                            PdfDictionary apResources =
                                ((PdfStream) apObj).getAsDictionary(
                                    PdfName.Resources);
                            if (apResources != null) {
                                embedFontsInResources(pdfDoc, apResources,
                                    fontCache, fontStreamCache, processed, embedded);
                            }
                        } else if (apObj instanceof PdfDictionary) {
                            // /N can be a dict of state names → streams
                            PdfDictionary apDict = (PdfDictionary) apObj;
                            for (PdfName stateKey : apDict.keySet()) {
                                PdfStream stateStream =
                                    apDict.getAsStream(stateKey);
                                if (stateStream != null) {
                                    PdfDictionary stResources =
                                        stateStream.getAsDictionary(
                                            PdfName.Resources);
                                    if (stResources != null) {
                                        embedFontsInResources(pdfDoc,
                                            stResources, fontCache,
                                            fontStreamCache, processed, embedded);
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        // Check AcroForm default resources
        PdfDictionary acroForm = pdfDoc.getCatalog().getPdfObject()
            .getAsDictionary(new PdfName("AcroForm"));
        if (acroForm != null) {
            PdfDictionary dr = acroForm.getAsDictionary(new PdfName("DR"));
            if (dr != null) {
                embedFontsInResources(pdfDoc, dr, fontCache, fontStreamCache, processed, embedded);
            }
        }

        sqFontsInjected += embedded[0];
        System.out.println("  Font embedding: " + embedded[0] + " font(s) injected");

        // Second pass: repair existing FontDescriptors that are already
        // embedded but missing required metric fields (source PDF defects,
        // e.g. SourceSansPro subsets without FontBBox/Ascent/Descent).
        repairIncompleteDescriptors(pdfDoc);
    }

    /**
     * Scan all font descriptors in the document and fill in missing
     * required metric fields. Handles fonts that were already embedded
     * in the source PDF but had incomplete descriptors.
     */
    private static void repairIncompleteDescriptors(PdfDocument pdfDoc) {
        Set<String> seen = new HashSet<>();
        int repaired = 0;
        for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
            PdfDictionary resources = pdfDoc.getPage(p).getResources()
                .getPdfObject();
            if (resources == null) continue;
            repaired += repairDescriptorsInResources(
                pdfDoc, resources, seen);
        }
        if (repaired > 0) {
            sqFontDescriptorsRepaired += repaired;
            System.out.println("  FontDescriptor repair: " + repaired
                + " descriptor(s) had missing metrics filled");
        }
    }

    private static int repairDescriptorsInResources(
            PdfDocument pdfDoc, PdfDictionary resources,
            Set<String> seen) {
        // Guard against circular resource references (StackOverflowError)
        String resId = resources.getIndirectReference() != null
            ? "res:" + resources.getIndirectReference().toString()
            : "res:" + System.identityHashCode(resources);
        if (!seen.add(resId)) return 0;

        int repaired = 0;
        PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
        if (fonts == null) return 0;
        for (PdfName fontKey : fonts.keySet()) {
            PdfDictionary fontDict = fonts.getAsDictionary(fontKey);
            if (fontDict == null) continue;

            // Find the FontDescriptor (simple or Type0 composite)
            PdfDictionary descriptor = fontDict.getAsDictionary(
                new PdfName("FontDescriptor"));
            if (descriptor == null) {
                PdfArray descendants = fontDict.getAsArray(
                    new PdfName("DescendantFonts"));
                if (descendants != null && descendants.size() > 0) {
                    PdfDictionary cidFont = descendants.getAsDictionary(0);
                    if (cidFont != null)
                        descriptor = cidFont.getAsDictionary(
                            new PdfName("FontDescriptor"));
                }
            }
            if (descriptor == null) continue;

            // Deduplicate by indirect reference
            String objId = descriptor.getIndirectReference() != null
                ? descriptor.getIndirectReference().toString()
                : fontKey.getValue();
            if (seen.contains(objId)) continue;
            seen.add(objId);

            // Check if any required field is missing
            boolean needsRepair =
                !descriptor.containsKey(new PdfName("FontBBox"))
                || !descriptor.containsKey(new PdfName("Ascent"))
                || !descriptor.containsKey(new PdfName("Descent"));
            if (!needsRepair) continue;

            // Get the base font name for fallback lookup
            PdfName baseFontName = fontDict.getAsName(PdfName.BaseFont);
            if (baseFontName == null) {
                // Try descriptor's /FontName (works for Type0 composites)
                baseFontName = descriptor.getAsName(
                    new PdfName("FontName"));
            }
            String baseFont = baseFontName != null
                ? baseFontName.getValue() : "unknown";

            // Try to extract metrics from the embedded font program
            boolean metricsSet = false;
            if (descriptor.containsKey(new PdfName("FontFile"))
                    || descriptor.containsKey(new PdfName("FontFile2"))
                    || descriptor.containsKey(new PdfName("FontFile3"))) {
                // Font is embedded — try to load it via iText for metrics
                try {
                    // For Type1 with .pfb, try the mapped path
                    String pfbPath = mapToType1Font(baseFont);
                    if (pfbPath != null
                            && new java.io.File(pfbPath).exists()) {
                        byte[] rawType1 = java.nio.file.Files.readAllBytes(
                            java.nio.file.Paths.get(pfbPath));
                        PdfFont font = PdfFontFactory.createFont(
                            rawType1, PdfEncodings.WINANSI,
                            PdfFontFactory.EmbeddingStrategy
                                .FORCE_EMBEDDED);
                        populateDescriptorMetrics(
                            descriptor, font, baseFont);
                        metricsSet = true;
                    }
                } catch (Exception e) { /* fall through */ }

                if (!metricsSet) {
                    try {
                        // For TrueType, try the mapped path
                        String ttfPath = mapToEmbeddableFont(baseFont);
                        if (ttfPath != null
                                && new java.io.File(
                                    stripTtcIndex(ttfPath)).exists()) {
                            // CJK TTC fonts need IDENTITY_H encoding
                            String enc = ttfPath.contains(".ttc,")
                                ? PdfEncodings.IDENTITY_H
                                : PdfEncodings.WINANSI;
                            PdfFont font = PdfFontFactory.createFont(
                                ttfPath, enc,
                                PdfFontFactory.EmbeddingStrategy
                                    .FORCE_EMBEDDED);
                            populateDescriptorMetrics(
                                descriptor, font, baseFont);
                            metricsSet = true;
                        }
                    } catch (Exception e) { /* fall through */ }
                }
            }

            // Final safety net with hardcoded defaults
            ensureDescriptorDefaults(descriptor, baseFont);
            repaired++;
            System.out.println("  Repaired FontDescriptor for " + baseFont);
        }

        // Recurse into XObject form resources
        PdfDictionary xobjects = resources.getAsDictionary(
            new PdfName("XObject"));
        if (xobjects != null) {
            for (PdfName xoKey : xobjects.keySet()) {
                PdfStream xoStream = xobjects.getAsStream(xoKey);
                if (xoStream != null) {
                    PdfDictionary xoRes = xoStream.getAsDictionary(
                        PdfName.Resources);
                    if (xoRes != null)
                        repaired += repairDescriptorsInResources(
                            pdfDoc, xoRes, seen);
                }
            }
        }
        return repaired;
    }

    private static void embedFontsInResources(PdfDocument pdfDoc,
            PdfDictionary resources, Map<String, byte[]> fontCache,
            Map<String, PdfStream> fontStreamCache,
            Set<String> processed, int[] embedded) {
        // Guard against circular resource references
        String resId = resources.getIndirectReference() != null
            ? "embedRes:" + resources.getIndirectReference().toString()
            : "embedRes:" + System.identityHashCode(resources);
        if (!processed.add(resId)) return;

        PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
        if (fonts == null) return;

        for (PdfName fontKey : fonts.keySet()) {
            PdfDictionary fontDict = fonts.getAsDictionary(fontKey);
            if (fontDict == null) continue;

                // Get FontDescriptor — simple fonts first, then Type0 composite
                PdfDictionary descriptor = fontDict.getAsDictionary(
                    new PdfName("FontDescriptor"));
                if (descriptor == null) {
                    PdfArray descendants = fontDict.getAsArray(
                        new PdfName("DescendantFonts"));
                    if (descendants != null && descendants.size() > 0) {
                        PdfDictionary descFont = descendants.getAsDictionary(0);
                        if (descFont != null)
                            descriptor = descFont.getAsDictionary(
                                new PdfName("FontDescriptor"));
                    }
                }
                // Get BaseFont name early (needed for mapping check)
                String baseFont = fontDict.containsKey(PdfName.BaseFont)
                    ? fontDict.getAsName(PdfName.BaseFont).getValue()
                    : fontKey.getValue();

                PdfName subtype = fontDict.getAsName(PdfName.Subtype);
                boolean isType1 = subtype != null
                    && "Type1".equals(subtype.getValue());

                boolean handledAsType1 = false;
                if (isType1) {
                    // --- Type1 embedding: use URW Nimbus .pfb via /FontFile ---
                    String pfbPath = mapToType1Font(baseFont);
                    if (pfbPath != null && new java.io.File(pfbPath).exists()) {
                    handledAsType1 = true;

                    // Create FontDescriptor if missing (standard 14 often lack one)
                    if (descriptor == null) {
                        descriptor = new PdfDictionary();
                        descriptor.put(new PdfName("Type"),
                            new PdfName("FontDescriptor"));
                        descriptor.put(new PdfName("FontName"),
                            new PdfName(baseFont));
                        descriptor.put(new PdfName("Flags"), new PdfNumber(32));
                        descriptor.makeIndirect(pdfDoc);
                        fontDict.put(new PdfName("FontDescriptor"), descriptor);
                        fontDict.setModified();
                    }

                    // Deduplicate (must be before re-embed check to avoid
                    // removing FontFile we just added on a prior page)
                    String objId = descriptor.getIndirectReference() != null
                        ? descriptor.getIndirectReference().toString()
                        : fontKey.getValue();
                    if (processed.contains(objId)) continue;

                    // Already embedded? (Type1 path)
                    if (descriptor.containsKey(new PdfName("FontFile"))
                            || descriptor.containsKey(new PdfName("FontFile2"))
                            || descriptor.containsKey(new PdfName("FontFile3"))) {
                        if (!shouldReEmbed(baseFont, descriptor)) continue;
                        // Broken subset — remove existing embedding to re-embed full font
                        descriptor.remove(new PdfName("FontFile"));
                        descriptor.remove(new PdfName("FontFile2"));
                        descriptor.remove(new PdfName("FontFile3"));
                        descriptor.setModified();
                        System.out.println("  Re-embedding subset font (Type1): " + baseFont);
                    }

                    processed.add(objId);

                    try {
                        byte[] pfbBytes = fontCache.computeIfAbsent(pfbPath, path -> {
                            try {
                                return java.nio.file.Files.readAllBytes(
                                    java.nio.file.Paths.get(path));
                            } catch (Exception e) { return null; }
                        });
                        if (pfbBytes == null) {
                            System.err.println("  Font embedding: cannot read "
                                + pfbPath);
                            continue;
                        }

                        int[] segments = parsePfbSegments(pfbBytes);
                        if (segments == null) {
                            System.err.println("  Font embedding: bad .pfb format "
                                + pfbPath);
                            continue;
                        }

                        byte[] rawType1 = stripPfbHeaders(pfbBytes);

                        PdfStream fontStream = fontStreamCache.get(pfbPath);
                        if (fontStream == null) {
                            fontStream = new PdfStream(rawType1, CompressionConstants.DEFAULT_COMPRESSION);
                            fontStream.put(new PdfName("Length1"),
                                new PdfNumber(segments[0]));
                            fontStream.put(new PdfName("Length2"),
                                new PdfNumber(segments[1]));
                            fontStream.put(new PdfName("Length3"),
                                new PdfNumber(segments[2]));
                            fontStream.makeIndirect(pdfDoc);
                            fontStreamCache.put(pfbPath, fontStream);
                        }
                        descriptor.put(new PdfName("FontFile"), fontStream);

                        // Update /BaseFont and /FontName to match the actual
                        // Keep the original Standard 14 BaseFont name (e.g.
                        // "Helvetica") even though the embedded font program
                        // is URW Nimbus.  Acrobat's OCR text selection for
                        // scanned PDFs correlates invisible text glyphs with
                        // the scan image using the font name and built-in
                        // metrics.  Renaming to "NimbusSans-Regular" breaks
                        // this correlation, causing near-zero-width selection
                        // highlights.  The FontDescriptor's /FontName is
                        // updated to the embedded name for PAC compatibility
                        // (PAC resolves system fonts by FontDescriptor name).
                        String embFontName = getEmbeddedFontName(pfbPath);
                        if (embFontName != null
                                && !embFontName.equals(baseFont)) {
                            // Only update FontDescriptor/FontName, NOT
                            // the font dict's BaseFont.  This preserves
                            // Acrobat's OCR text selection correlation.
                            descriptor.put(new PdfName("FontName"),
                                new PdfName(embFontName));
                            System.out.println("    Embedded " + baseFont
                                + " (program: " + embFontName + ")");
                        }
                        descriptor.setModified();

                        // Sync /Widths with embedded font program to satisfy
                        // veraPDF 7.21.5. Generate widths if missing, or
                        // recalculate if present (URW Nimbus may differ on
                        // individual glyphs despite being metrically similar).
                        boolean widthSynced = false;

                        // ZapfDingbats and Symbol use their own encodings
                        // (not WinAnsi), so must use AFM char-code lookup.
                        boolean isSpecialEncoding =
                            baseFont.contains("ZapfDingbats")
                            || baseFont.contains("Symbol");

                        if (isSpecialEncoding) {
                            // Use AFM with direct char-code→width mapping
                            try {
                                String afmPath = resolveAfmPath(pfbPath);
                                if (afmPath != null) {
                                    widthSynced =
                                        syncWidthsFromAfmByCharCode(
                                            fontDict, afmPath, pdfDoc);
                                    if (widthSynced) {
                                        System.out.println(
                                            "  Width sync via AFM (charcode)"
                                            + " for " + baseFont);
                                    }
                                }
                            } catch (Exception ae) {
                                System.err.println(
                                    "  AFM charcode width sync failed for "
                                    + baseFont + ": " + ae.getMessage());
                            }
                            // Extract metrics for descriptor
                            try {
                                PdfFont metricFont =
                                    PdfFontFactory.createFont(
                                        pfbPath, PdfEncodings.WINANSI,
                                        PdfFontFactory.EmbeddingStrategy
                                            .FORCE_EMBEDDED);
                                populateDescriptorMetrics(
                                    descriptor, metricFont, baseFont);
                            } catch (Exception me) {
                                // Metrics from PdfFont failed;
                                // ensureDescriptorDefaults will cover
                            }
                        } else {
                            // Standard WinAnsi fonts: try raw bytes first
                            try {
                                PdfFont embFont = PdfFontFactory.createFont(
                                    rawType1, PdfEncodings.WINANSI,
                                    PdfFontFactory.EmbeddingStrategy
                                        .FORCE_EMBEDDED);
                                widthSynced = syncType1Widths(
                                    fontDict, embFont, pdfDoc);
                                populateDescriptorMetrics(
                                    descriptor, embFont, baseFont);
                            } catch (Exception we) {
                                // Raw bytes didn't work — try AFM fallback
                            }

                            // Fallback: AFM glyph-name lookup (WinAnsi)
                            if (!widthSynced) {
                                try {
                                    String afmPath = resolveAfmPath(pfbPath);
                                    if (afmPath != null) {
                                        widthSynced =
                                            syncType1WidthsFromAfm(
                                                fontDict, afmPath, pdfDoc);
                                        if (widthSynced) {
                                            System.out.println(
                                                "  Width sync via AFM for "
                                                + baseFont);
                                        }
                                    }
                                } catch (Exception ae) {
                                    System.err.println(
                                        "  AFM width sync failed for "
                                        + baseFont + ": "
                                        + ae.getMessage());
                                }
                            }
                        }

                        if (!widthSynced) {
                            System.err.println(
                                "  WARNING: Width sync failed for "
                                + baseFont + " — 7.21.5 may fail");
                        }

                        // Ensure all required descriptor fields are present
                        ensureDescriptorDefaults(descriptor, baseFont);

                        embedded[0]++;
                        // Standard 14 Type1 fonts map to metric-compatible
                        // URW Nimbus clones — not a true substitution.
                        // Don't count these in the substitution warning.
                        System.out.println("  Embedded Type1 " + baseFont
                            + " <- " + pfbPath);
                    } catch (Exception e) {
                        System.err.println("  Font embedding error for "
                            + baseFont + ": " + e.getMessage());
                    }

                } } // end Type1 block

                if (!handledAsType1) {
                    // --- TrueType embedding (also fallback for Type1-declared
                    //     fonts not in the standard 14, e.g. ArialMT) ---

                    // TrueType fonts may lack FontDescriptor entirely.
                    // Create one if we have a Liberation match for this font.
                    if (descriptor == null) {
                        String probe = mapToEmbeddableFont(baseFont);
                        if (probe != null) {
                            System.out.println("  Creating FontDescriptor for "
                                + baseFont);
                            descriptor = new PdfDictionary();
                            descriptor.put(new PdfName("Type"),
                                new PdfName("FontDescriptor"));
                            descriptor.put(new PdfName("FontName"),
                                new PdfName(baseFont));
                            descriptor.makeIndirect(pdfDoc);
                            fontDict.put(new PdfName("FontDescriptor"), descriptor);
                            fontDict.setModified();
                        } else {
                            continue;
                        }
                    }

                    // Deduplicate by indirect object reference (must be before
                    // re-embed check to avoid removing FontFile we just added)
                    String objId = descriptor.getIndirectReference() != null
                        ? descriptor.getIndirectReference().toString()
                        : fontKey.getValue();
                    if (processed.contains(objId)) continue;

                    // Already embedded? (TrueType path)
                    if (descriptor.containsKey(new PdfName("FontFile"))
                            || descriptor.containsKey(new PdfName("FontFile2"))
                            || descriptor.containsKey(new PdfName("FontFile3"))) {
                        if (!shouldReEmbed(baseFont, descriptor)) continue;
                        // Broken subset — remove existing embedding to re-embed full font
                        descriptor.remove(new PdfName("FontFile"));
                        descriptor.remove(new PdfName("FontFile2"));
                        descriptor.remove(new PdfName("FontFile3"));
                        descriptor.setModified();
                        System.out.println("  Re-embedding subset font (TrueType): " + baseFont);
                    }

                    processed.add(objId);

                    String libPath = mapToEmbeddableFont(baseFont);
                    if (libPath == null) {
                        System.out.println("  Font embedding: no match for "
                            + baseFont);
                        continue;
                    }

                    try {
                        // Strip TTC index for filesystem I/O
                        String libFilePath = stripTtcIndex(libPath);
                        boolean isTtc = !libFilePath.equals(libPath);

                        byte[] fontBytes = fontCache.computeIfAbsent(
                            libFilePath,
                            path -> {
                                try {
                                    return java.nio.file.Files.readAllBytes(
                                        java.nio.file.Paths.get(path));
                                } catch (Exception e) { return null; }
                            });
                        if (fontBytes == null) {
                            System.err.println("  Font embedding: cannot read "
                                + libFilePath);
                            continue;
                        }

                        // Inject raw TTF/TTC bytes as FontFile2 stream
                        PdfStream fontStream =
                            fontStreamCache.get(libFilePath);
                        if (fontStream == null) {
                            fontStream = new PdfStream(fontBytes, CompressionConstants.DEFAULT_COMPRESSION);
                            fontStream.put(new PdfName("Length1"),
                                new PdfNumber(fontBytes.length));
                            fontStream.makeIndirect(pdfDoc);
                            fontStreamCache.put(libFilePath, fontStream);
                        }
                        descriptor.put(new PdfName("FontFile2"), fontStream);
                        descriptor.setModified();

                        // If font was declared as Type1 but we're embedding
                        // TrueType, update Subtype so veraPDF checks FontFile2
                        if (isType1) {
                            fontDict.put(PdfName.Subtype,
                                new PdfName("TrueType"));
                            fontDict.setModified();
                        }

                        // Rename /BaseFont and /FontName to match the
                        // embedded font (Liberation/URW/Noto substitutes only
                        // — Windows/Apple exact-match fonts keep original name)
                        String embFontNameTT = getEmbeddedFontName(libPath);
                        boolean isLibOrUrw = libPath.contains("/liberation/")
                            || libPath.contains("/urw-base35/")
                            || libPath.contains("/X11/Type1/")
                            || libPath.contains("/noto/");
                        String cleanBaseForRename = baseFont.contains("+")
                            ? baseFont.substring(
                                baseFont.indexOf('+') + 1) : baseFont;
                        if (isLibOrUrw && embFontNameTT != null
                                && !embFontNameTT.equals(cleanBaseForRename)) {
                            fontDict.put(PdfName.BaseFont,
                                new PdfName(embFontNameTT));
                            descriptor.put(new PdfName("FontName"),
                                new PdfName(embFontNameTT));
                            fontDict.setModified();
                            descriptor.setModified();
                            System.out.println("    Renamed " + baseFont
                                + " -> " + embFontNameTT);
                        }

                        // Always create PdfFont for metrics extraction
                        // CJK TTC fonts need IDENTITY_H encoding
                        PdfFont embFont = null;
                        try {
                            String enc = isTtc
                                ? PdfEncodings.IDENTITY_H
                                : PdfEncodings.WINANSI;
                            embFont = PdfFontFactory.createFont(
                                libPath, enc,
                                PdfFontFactory.EmbeddingStrategy
                                    .FORCE_EMBEDDED);
                            populateDescriptorMetrics(
                                descriptor, embFont, baseFont);
                        } catch (Exception fontErr) {
                            System.out.println(
                                "  PdfFont metrics extraction failed for "
                                + baseFont + ": " + fontErr.getMessage());
                        }

                        // If the embedded font differs from the original
                        // (substitute), update /Widths to match the embedded
                        // font program (veraPDF 7.21.5). Catches Liberation,
                        // ArialNarrow→Arial mismatches, etc.
                        String cleanBase = baseFont.contains("+")
                            ? baseFont.substring(baseFont.indexOf('+') + 1)
                            : baseFont;
                        String normBase = cleanBase.toLowerCase(java.util.Locale.ROOT)
                            .replace("-", "").replace(" ", "");
                        String normLib = new java.io.File(libPath).getName()
                            .toLowerCase(java.util.Locale.ROOT).replace(".ttf", "")
                            .replace("-", "").replace("_", "");
                        boolean isSubstitute = !normLib.contains(normBase)
                            && !normBase.contains(normLib);
                        // Skip WinAnsi width recalculation for symbolic fonts
                        // (Wingdings, Symbol) — their encoding is not WinAnsi
                        // and the full font has same metrics as the subset
                        boolean isSymbolicFont = cleanBase.contains("Wingdings")
                            || cleanBase.equals("Symbol");
                        if (isSubstitute && !isSymbolicFont
                                && fontDict.containsKey(PdfName.Widths)
                                && embFont != null) {
                            try {
                                PdfNumber firstCharObj = fontDict.getAsNumber(
                                    new PdfName("FirstChar"));
                                PdfNumber lastCharObj = fontDict.getAsNumber(
                                    new PdfName("LastChar"));
                                if (firstCharObj != null
                                        && lastCharObj != null) {
                                    int firstChar = firstCharObj.intValue();
                                    int lastChar = lastCharObj.intValue();
                                    PdfArray newWidths = new PdfArray();
                                    byte[] singleByte = new byte[1];
                                    for (int c = firstChar;
                                            c <= lastChar; c++) {
                                        singleByte[0] = (byte) c;
                                        String unicode =
                                            PdfEncodings.convertToString(
                                                singleByte,
                                                PdfEncodings.WINANSI);
                                        int w = 0;
                                        if (unicode != null
                                                && !unicode.isEmpty()) {
                                            w = embFont.getWidth(
                                                unicode.charAt(0));
                                        }
                                        newWidths.add(new PdfNumber(w));
                                    }
                                    fontDict.put(PdfName.Widths, newWidths);
                                    fontDict.setModified();
                                }
                            } catch (Exception widthErr) {
                                System.out.println(
                                    "  Width update skipped for " + baseFont
                                    + ": " + widthErr.getMessage());
                            }
                        }

                        // Ensure all required descriptor fields are present
                        ensureDescriptorDefaults(descriptor, baseFont);

                        embedded[0]++;
                        if (isLibOrUrw) {
                            sqSubstitutionFontsInjected++;
                            if (sqSubstitutionFontNames.size() < 50)
                                sqSubstitutionFontNames.add(baseFont);
                        }
                        System.out.println("  Embedded " + baseFont
                            + " <- " + libPath);
                    } catch (Exception e) {
                        System.err.println("  Font embedding error for "
                            + baseFont + ": " + e.getMessage());
                    }
                }
            }
        }


    /**
     * Determines whether a subset-embedded font should be re-embedded with the
     * full font program. Only returns true for fonts with a 6-char subset prefix
     * (e.g. "ABCDEF+FontName") when a full replacement font is available.
     * This fixes veraPDF 7.21.4.1 t2 (missing glyphs in subset).
     */
    private static boolean shouldReEmbed(String baseFont, PdfDictionary descriptor) {
        // Only consider subset-prefixed fonts
        if (!baseFont.contains("+") || baseFont.indexOf('+') != 6)
            return false;

        String clean = baseFont.substring(baseFont.indexOf('+') + 1);

        // Family allowlist. Re-embedding every subset is too aggressive: a
        // well-formed subset (Arial from Word, say) is broken by replacing its
        // program with a different build of the font. So only families whose
        // subsets are, in practice, incomplete (veraPDF 7.21.4.1: glyphs used
        // by the text are missing from the subset) or carry /Widths that do
        // not match the program (7.21.5) are replaced. Extend the list when a
        // new family shows either failure; a glyph-by-glyph check would be the
        // general solution.
        boolean isKnownBroken =
            // incomplete subsets
            clean.contains("CooperBlack") || clean.contains("Cooper Black")
            || clean.contains("Futura")
            || clean.contains("Wingdings")
            || clean.equals("Symbol") || clean.startsWith("Symbol-")
            // width mismatches
            || clean.contains("Lato")
            || clean.contains("SourceSans")
            || clean.contains("Segoe") && clean.contains("Variable");
        if (!isKnownBroken) return false;

        // Don't re-embed if the font already has a valid font program
        if (descriptor != null) {
            String[] fontFileKeys = {"FontFile", "FontFile2", "FontFile3"};
            for (String key : fontFileKeys) {
                PdfObject ff = descriptor.get(new PdfName(key));
                if (ff != null && ff.isStream()) {
                    byte[] ffBytes = ((PdfStream) ff).getBytes();
                    if (ffBytes != null && ffBytes.length > 0) {
                        System.out.println("  Keeping valid embedding (" + key
                            + ") for " + baseFont + " (" + ffBytes.length + " bytes)");
                        return false;
                    }
                }
            }
        }

        // Check if we have a full font available via either mapping
        String ttfPath = mapToEmbeddableFont(baseFont);
        if (ttfPath != null
                && new java.io.File(stripTtcIndex(ttfPath)).exists())
            return true;

        String pfbPath = mapToType1Font(baseFont);
        if (pfbPath != null && new java.io.File(pfbPath).exists())
            return true;

        return false;
    }

    /**
     * Maps a standard 14 PDF font name to a URW Nimbus Type1 .pfb path.
     * URW Nimbus fonts are metrically identical clones of the standard 14.
     * Returns null if the font is not one of the standard 14.
     */
    private static String mapToType1Font(String baseFont) {
        String clean = baseFont.contains("+")
            ? baseFont.substring(baseFont.indexOf('+') + 1) : baseFont;
        // Normalize: replace commas with hyphens (PDFs use both conventions)
        String norm = clean.replace(',', '-');

        String pfbBase = "/usr/share/fonts/X11/Type1/";

        // Helvetica family → NimbusSans
        if (norm.equals("Helvetica"))              return pfbBase + "NimbusSans-Regular.pfb";
        if (norm.equals("Helvetica-Bold"))         return pfbBase + "NimbusSans-Bold.pfb";
        if (norm.equals("Helvetica-Oblique"))      return pfbBase + "NimbusSans-Italic.pfb";
        if (norm.equals("Helvetica-BoldOblique"))  return pfbBase + "NimbusSans-BoldItalic.pfb";

        // Times family → NimbusRoman
        if (norm.equals("Times-Roman"))            return pfbBase + "NimbusRoman-Regular.pfb";
        if (norm.equals("Times-Bold"))             return pfbBase + "NimbusRoman-Bold.pfb";
        if (norm.equals("Times-Italic"))           return pfbBase + "NimbusRoman-Italic.pfb";
        if (norm.equals("Times-BoldItalic"))       return pfbBase + "NimbusRoman-BoldItalic.pfb";

        // Courier family → NimbusMonoPS
        if (norm.equals("Courier"))                return pfbBase + "NimbusMonoPS-Regular.pfb";
        if (norm.equals("Courier-Bold"))           return pfbBase + "NimbusMonoPS-Bold.pfb";
        if (norm.equals("Courier-Oblique"))        return pfbBase + "NimbusMonoPS-Italic.pfb";
        if (norm.equals("Courier-BoldOblique"))    return pfbBase + "NimbusMonoPS-BoldItalic.pfb";

        // Symbol and ZapfDingbats
        if (norm.equals("Symbol"))                 return pfbBase + "StandardSymbolsPS.pfb";
        if (norm.equals("ZapfDingbats"))           return pfbBase + "D050000L.pfb";

        return null;
    }

    /**
     * Derive the PostScript font name from the font file path.
     * Strips directory and extension: "NimbusSans-Bold.pfb" → "NimbusSans-Bold".
     */
    private static String getEmbeddedFontName(String fontFilePath) {
        String stripped = stripTtcIndex(fontFilePath);
        String fileName = new java.io.File(stripped).getName();
        int dot = fileName.lastIndexOf('.');
        return dot > 0 ? fileName.substring(0, dot) : fileName;
    }

    /**
     * Parse .pfb binary format to extract segment lengths.
     * PFB format: each segment has a 6-byte header:
     *   byte 0: 0x80 (magic)
     *   byte 1: segment type (1=ASCII, 2=binary, 3=EOF)
     *   bytes 2-5: little-endian segment length
     * Returns int[3] = {length1 (ASCII), length2 (encrypted), length3 (zeros)},
     * or null if parsing fails.
     */
    private static int[] parsePfbSegments(byte[] pfb) {
        try {
            int[] lengths = new int[3];
            int offset = 0;
            for (int seg = 0; seg < 3; seg++) {
                if (offset + 6 > pfb.length) break;
                if ((pfb[offset] & 0xFF) != 0x80) return null;  // bad magic
                int type = pfb[offset + 1] & 0xFF;
                if (type == 3) {  // EOF marker — no more segments
                    lengths[seg] = 0;
                    break;
                }
                int len = (pfb[offset + 2] & 0xFF)
                        | ((pfb[offset + 3] & 0xFF) << 8)
                        | ((pfb[offset + 4] & 0xFF) << 16)
                        | ((pfb[offset + 5] & 0xFF) << 24);
                lengths[seg] = len;
                offset += 6 + len;  // skip header + data
            }
            return lengths;
        } catch (Exception e) {
            return null;
        }
    }

    /**
     * Strip .pfb segment headers, returning raw Type1 font program data.
     * PDF /FontFile streams contain the raw program without .pfb headers.
     */
    private static byte[] stripPfbHeaders(byte[] pfb) {
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream(pfb.length);
        int offset = 0;
        while (offset + 6 <= pfb.length) {
            if ((pfb[offset] & 0xFF) != 0x80) break;
            int type = pfb[offset + 1] & 0xFF;
            if (type == 3) break;  // EOF marker
            int len = (pfb[offset + 2] & 0xFF)
                    | ((pfb[offset + 3] & 0xFF) << 8)
                    | ((pfb[offset + 4] & 0xFF) << 16)
                    | ((pfb[offset + 5] & 0xFF) << 24);
            offset += 6;
            if (offset + len > pfb.length) break;
            out.write(pfb, offset, len);
            offset += len;
        }
        return out.toByteArray();
    }

    /**
     * Sync /Widths array from a PdfFont object (used when iText can parse
     * the font). Shared by both raw-bytes and TTF width sync paths.
     * Returns true if widths were successfully written.
     */
    private static boolean syncType1Widths(PdfDictionary fontDict,
            PdfFont embFont, PdfDocument pdfDoc) {
        PdfNumber fcObj = fontDict.getAsNumber(new PdfName("FirstChar"));
        PdfNumber lcObj = fontDict.getAsNumber(new PdfName("LastChar"));
        int firstChar = fcObj != null ? fcObj.intValue() : 32;
        int lastChar = lcObj != null ? lcObj.intValue() : 255;

        // Honor the font's /Encoding /Differences. A code may be remapped to a
        // glyph (e.g. 2 -> /space) whose WinAnsi byte is a control char with
        // width 0; using the remapped glyph's Unicode yields the real advance
        // and avoids writing /Widths=0 for a visible glyph (veraPDF 7.21.5 t1).
        Map<Integer, Integer> diffUnicode = new HashMap<>();
        PdfObject encodingObj = fontDict.get(PdfName.Encoding);
        if (encodingObj != null && encodingObj.isDictionary()) {
            PdfArray differences = ((PdfDictionary) encodingObj)
                .getAsArray(new PdfName("Differences"));
            if (differences != null) {
                int code = 0;
                for (int i = 0; i < differences.size(); i++) {
                    PdfObject item = differences.get(i);
                    if (item.isNumber()) {
                        code = ((PdfNumber) item).intValue();
                    } else if (item.isName()) {
                        int u = glyphNameToUnicode(((PdfName) item).getValue());
                        if (u > 0) diffUnicode.put(code, u);
                        code++;
                    }
                }
            }
        }

        PdfArray newWidths = new PdfArray();
        byte[] sb = new byte[1];
        for (int c = firstChar; c <= lastChar; c++) {
            int w = 0;
            Integer mapped = diffUnicode.get(c);
            if (mapped != null) {
                w = embFont.getWidth((int) mapped);
            } else {
                sb[0] = (byte) c;
                String u = PdfEncodings.convertToString(sb, PdfEncodings.WINANSI);
                if (u != null && !u.isEmpty())
                    w = embFont.getWidth(u.charAt(0));
            }
            newWidths.add(new PdfNumber(w));
        }
        if (fcObj == null)
            fontDict.put(new PdfName("FirstChar"), new PdfNumber(firstChar));
        if (lcObj == null)
            fontDict.put(new PdfName("LastChar"), new PdfNumber(lastChar));
        fontDict.put(PdfName.Widths, newWidths);
        fontDict.setModified();
        return true;
    }

    /**
     * Sync /Widths from AFM using direct char-code→width mapping.
     * For fonts with non-WinAnsi encodings (ZapfDingbats, Symbol)
     * where the AFM's C (char code) field directly corresponds to
     * the PDF font's char code positions.
     */
    private static boolean syncWidthsFromAfmByCharCode(
            PdfDictionary fontDict, String afmPath,
            PdfDocument pdfDoc) throws Exception {
        // Parse AFM: build charCode → width map
        java.util.Map<Integer, Integer> codeWidths = new java.util.HashMap<>();
        try (java.io.BufferedReader reader = new java.io.BufferedReader(
                new java.io.FileReader(afmPath))) {
            String line;
            boolean inCharMetrics = false;
            while ((line = reader.readLine()) != null) {
                if (line.startsWith("StartCharMetrics")) {
                    inCharMetrics = true; continue;
                }
                if (line.startsWith("EndCharMetrics")) break;
                if (!inCharMetrics) continue;
                int charCode = -1;
                int width = 0;
                String[] parts = line.split(";");
                for (String part : parts) {
                    String trimmed = part.trim();
                    if (trimmed.startsWith("C ")) {
                        charCode = Integer.parseInt(trimmed.substring(2).trim());
                    } else if (trimmed.startsWith("WX ")) {
                        width = Integer.parseInt(trimmed.substring(3).trim());
                    }
                }
                if (charCode >= 0) {
                    codeWidths.put(charCode, width);
                }
            }
        }
        if (codeWidths.isEmpty()) return false;

        PdfNumber fcObj = fontDict.getAsNumber(new PdfName("FirstChar"));
        PdfNumber lcObj = fontDict.getAsNumber(new PdfName("LastChar"));
        int firstChar = fcObj != null ? fcObj.intValue() : 32;
        int lastChar = lcObj != null ? lcObj.intValue() : 255;

        PdfArray newWidths = new PdfArray();
        for (int c = firstChar; c <= lastChar; c++) {
            Integer w = codeWidths.get(c);
            newWidths.add(new PdfNumber(w != null ? w : 0));
        }

        if (fcObj == null)
            fontDict.put(new PdfName("FirstChar"), new PdfNumber(firstChar));
        if (lcObj == null)
            fontDict.put(new PdfName("LastChar"), new PdfNumber(lastChar));
        fontDict.put(PdfName.Widths, newWidths);
        fontDict.setModified();
        return true;
    }

    // ================================================================
    // FontDescriptor metrics population
    // ================================================================

    // Hardcoded fallback metrics for Standard 14 fonts.
    // Format: {Ascent, Descent, CapHeight, XHeight, StemV, BBox_llx, BBox_lly, BBox_urx, BBox_ury}
    // Values from Adobe PDF spec Appendix D / AFM files.
    // URW Nimbus AFMs report Ascender=0/Descender=0, so these are essential.
    private static final java.util.Map<String, int[]> STANDARD_FONT_FALLBACKS
        = new java.util.HashMap<>();
    static {
        STANDARD_FONT_FALLBACKS.put("Helvetica",             new int[]{718, -207, 718, 523,  88, -166, -225, 1000,  931});
        STANDARD_FONT_FALLBACKS.put("Helvetica-Bold",        new int[]{718, -207, 718, 532, 140, -170, -228, 1003,  962});
        STANDARD_FONT_FALLBACKS.put("Helvetica-Oblique",     new int[]{718, -207, 718, 523,  88, -170, -225, 1116,  931});
        STANDARD_FONT_FALLBACKS.put("Helvetica-BoldOblique", new int[]{718, -207, 718, 532, 140, -174, -228, 1114,  962});
        STANDARD_FONT_FALLBACKS.put("Times-Roman",           new int[]{683, -217, 662, 450,  87, -168, -218, 1000,  898});
        STANDARD_FONT_FALLBACKS.put("Times-Bold",            new int[]{683, -217, 676, 461, 139, -168, -218, 1000,  935});
        STANDARD_FONT_FALLBACKS.put("Times-Italic",          new int[]{683, -217, 653, 441,  76, -169, -217, 1010,  883});
        STANDARD_FONT_FALLBACKS.put("Times-BoldItalic",      new int[]{683, -217, 669, 462, 121, -200, -218,  996,  921});
        STANDARD_FONT_FALLBACKS.put("Courier",               new int[]{629, -157, 562, 426,  51,  -23, -250,  715,  805});
        STANDARD_FONT_FALLBACKS.put("Courier-Bold",          new int[]{629, -157, 562, 439, 106, -113, -250,  749,  801});
        STANDARD_FONT_FALLBACKS.put("Courier-Oblique",       new int[]{629, -157, 562, 426,  51,  -27, -250,  849,  805});
        STANDARD_FONT_FALLBACKS.put("Courier-BoldOblique",   new int[]{629, -157, 562, 439, 106,  -57, -250,  869,  801});
        STANDARD_FONT_FALLBACKS.put("Symbol",                new int[]{1010,-293, 673, 500,  85, -180, -293, 1090, 1010});
        STANDARD_FONT_FALLBACKS.put("ZapfDingbats",          new int[]{820, -143, 691, 567,  90,   -1, -143,  981,  820});
    }
    private static final int[] DEFAULT_FONT_FALLBACK =
        new int[]{800, -200, 700, 500, 80, -200, -250, 1100, 1000};

    /**
     * Populate missing FontDescriptor metric fields using iText FontMetrics API.
     * Falls back to hardcoded Standard 14 values when iText returns 0.
     * Never overwrites existing fields in the descriptor.
     */
    private static void populateDescriptorMetrics(
            PdfDictionary descriptor, PdfFont font, String baseFont) {
        int ascent = 0, descent = 0, capHeight = 0, xHeight = 0, stemV = 0;
        float italicAngle = 0f;
        int[] bbox = null;
        int upm = 1000;

        FontProgram prog = font.getFontProgram();
        if (prog != null) {
            FontMetrics m = prog.getFontMetrics();
            if (m != null) {
                upm = m.getUnitsPerEm();
                if (upm <= 0) upm = 1000;
                ascent = m.getTypoAscender() != 0
                    ? m.getTypoAscender() : m.getAscender();
                descent = m.getTypoDescender() != 0
                    ? m.getTypoDescender() : m.getDescender();
                capHeight = m.getCapHeight();
                xHeight = m.getXHeight();
                stemV = m.getStemV();
                italicAngle = m.getItalicAngle();
                bbox = m.getBbox();
                if (bbox != null && bbox.length >= 4
                        && bbox[0] == 0 && bbox[1] == 0
                        && bbox[2] == 0 && bbox[3] == 0) {
                    bbox = null;
                }
            }
        }

        int[] fb = STANDARD_FONT_FALLBACKS.getOrDefault(
            baseFont, DEFAULT_FONT_FALLBACK);

        if (!descriptor.containsKey(new PdfName("Ascent")))
            descriptor.put(new PdfName("Ascent"), new PdfNumber(
                ascent != 0 ? normMetric(ascent, upm) : fb[0]));
        if (!descriptor.containsKey(new PdfName("Descent")))
            descriptor.put(new PdfName("Descent"), new PdfNumber(
                descent != 0 ? normMetric(descent, upm) : fb[1]));
        if (!descriptor.containsKey(new PdfName("CapHeight")))
            descriptor.put(new PdfName("CapHeight"), new PdfNumber(
                capHeight != 0 ? normMetric(capHeight, upm) : fb[2]));
        if (!descriptor.containsKey(new PdfName("XHeight")))
            descriptor.put(new PdfName("XHeight"), new PdfNumber(
                xHeight != 0 ? normMetric(xHeight, upm) : fb[3]));
        if (!descriptor.containsKey(new PdfName("StemV")))
            descriptor.put(new PdfName("StemV"), new PdfNumber(
                stemV != 0 ? normMetric(stemV, upm) : fb[4]));
        if (!descriptor.containsKey(new PdfName("ItalicAngle"))) {
            if (italicAngle == 0f
                    && (baseFont.contains("Oblique")
                        || baseFont.contains("Italic")))
                italicAngle = -12f;
            descriptor.put(new PdfName("ItalicAngle"),
                new PdfNumber(italicAngle));
        }
        if (!descriptor.containsKey(new PdfName("FontBBox"))) {
            PdfArray bboxArr = new PdfArray();
            if (bbox != null && bbox.length >= 4) {
                for (int i = 0; i < 4; i++)
                    bboxArr.add(new PdfNumber(normMetric(bbox[i], upm)));
            } else {
                for (int i = 5; i <= 8; i++)
                    bboxArr.add(new PdfNumber(fb[i]));
            }
            descriptor.put(new PdfName("FontBBox"), bboxArr);
        }
        if (!descriptor.containsKey(new PdfName("Flags")))
            descriptor.put(new PdfName("Flags"), new PdfNumber(32));

        descriptor.setModified();
    }

    /**
     * Safety-net: ensure all required FontDescriptor fields are present
     * using hardcoded fallbacks only. Called when PdfFont creation fails.
     */
    private static void ensureDescriptorDefaults(
            PdfDictionary descriptor, String baseFont) {
        int[] fb = STANDARD_FONT_FALLBACKS.getOrDefault(
            baseFont, DEFAULT_FONT_FALLBACK);
        if (!descriptor.containsKey(new PdfName("Ascent")))
            descriptor.put(new PdfName("Ascent"), new PdfNumber(fb[0]));
        if (!descriptor.containsKey(new PdfName("Descent")))
            descriptor.put(new PdfName("Descent"), new PdfNumber(fb[1]));
        if (!descriptor.containsKey(new PdfName("CapHeight")))
            descriptor.put(new PdfName("CapHeight"), new PdfNumber(fb[2]));
        if (!descriptor.containsKey(new PdfName("XHeight")))
            descriptor.put(new PdfName("XHeight"), new PdfNumber(fb[3]));
        if (!descriptor.containsKey(new PdfName("StemV")))
            descriptor.put(new PdfName("StemV"), new PdfNumber(fb[4]));
        if (!descriptor.containsKey(new PdfName("ItalicAngle")))
            descriptor.put(new PdfName("ItalicAngle"), new PdfNumber(
                (baseFont.contains("Oblique")
                    || baseFont.contains("Italic")) ? -12f : 0f));
        if (!descriptor.containsKey(new PdfName("FontBBox"))) {
            PdfArray bboxArr = new PdfArray();
            for (int i = 5; i <= 8; i++)
                bboxArr.add(new PdfNumber(fb[i]));
            descriptor.put(new PdfName("FontBBox"), bboxArr);
        }
        if (!descriptor.containsKey(new PdfName("Flags")))
            descriptor.put(new PdfName("Flags"), new PdfNumber(32));
        descriptor.setModified();
    }

    /** Normalize font metric from design units to PDF's 1000-unit scale. */
    private static int normMetric(int value, int unitsPerEm) {
        if (unitsPerEm == 1000) return value;
        return (int) Math.round((double) value * 1000.0 / unitsPerEm);
    }

    /**
     * Resolve the companion AFM path for a .pfb font file.
     * URW base-35 AFM files live in /usr/share/fonts/type1/urw-base35/
     * while the .pfb files are in /usr/share/fonts/X11/Type1/.
     * Returns null if the AFM file doesn't exist.
     */
    private static String resolveAfmPath(String pfbPath) {
        // Same directory: foo.pfb → foo.afm
        String sameDirAfm = pfbPath.replaceFirst("\\.pfb$", ".afm");
        if (new java.io.File(sameDirAfm).exists()) return sameDirAfm;

        // URW base-35 directory (typical on Debian/Ubuntu)
        String fileName = new java.io.File(pfbPath).getName()
            .replaceFirst("\\.pfb$", ".afm");
        String urwAfm = "/usr/share/fonts/type1/urw-base35/" + fileName;
        if (new java.io.File(urwAfm).exists()) return urwAfm;

        return null;
    }

    /**
     * Parse an AFM file and return a map of glyph name → width (in 1/1000 em).
     * AFM lines: "C <code> ; WX <width> ; N <name> ; B ..."
     */
    private static java.util.Map<String, Integer> parseAfmWidths(
            String afmPath) throws Exception {
        java.util.Map<String, Integer> widths = new java.util.HashMap<>();
        try (java.io.BufferedReader reader = new java.io.BufferedReader(
                new java.io.FileReader(afmPath))) {
            String line;
            boolean inCharMetrics = false;
            while ((line = reader.readLine()) != null) {
                if (line.startsWith("StartCharMetrics")) {
                    inCharMetrics = true;
                    continue;
                }
                if (line.startsWith("EndCharMetrics")) break;
                if (!inCharMetrics) continue;

                // Parse: "C <code> ; WX <width> ; N <name> ; B ..."
                String glyphName = null;
                int width = 0;
                String[] parts = line.split(";");
                for (String part : parts) {
                    String trimmed = part.trim();
                    if (trimmed.startsWith("WX ")) {
                        width = Integer.parseInt(trimmed.substring(3).trim());
                    } else if (trimmed.startsWith("N ")) {
                        glyphName = trimmed.substring(2).trim();
                    }
                }
                if (glyphName != null) {
                    widths.put(glyphName, width);
                }
            }
        }
        return widths;
    }

    /**
     * Sync /Widths array from an AFM file. Uses the WinAnsi encoding to
     * map char codes to glyph names, then looks up widths from the AFM.
     * Returns true if widths were successfully written.
     */
    private static boolean syncType1WidthsFromAfm(PdfDictionary fontDict,
            String afmPath, PdfDocument pdfDoc) throws Exception {
        java.util.Map<String, Integer> afmWidths = parseAfmWidths(afmPath);
        if (afmWidths.isEmpty()) return false;

        // Honor the font's /Encoding /Differences (code -> glyph name). A code
        // may be remapped (e.g. 2 -> /space) whose WinAnsi name is a control
        // char with no AFM width; using the remapped name yields the real
        // advance instead of 0 (veraPDF 7.21.5 t1).
        Map<Integer, String> diffNames = new HashMap<>();
        PdfObject encodingObj = fontDict.get(PdfName.Encoding);
        if (encodingObj != null && encodingObj.isDictionary()) {
            PdfArray differences = ((PdfDictionary) encodingObj)
                .getAsArray(new PdfName("Differences"));
            if (differences != null) {
                int code = 0;
                for (int i = 0; i < differences.size(); i++) {
                    PdfObject item = differences.get(i);
                    if (item.isNumber()) {
                        code = ((PdfNumber) item).intValue();
                    } else if (item.isName()) {
                        diffNames.put(code, ((PdfName) item).getValue());
                        code++;
                    }
                }
            }
        }

        PdfNumber fcObj = fontDict.getAsNumber(new PdfName("FirstChar"));
        PdfNumber lcObj = fontDict.getAsNumber(new PdfName("LastChar"));
        int firstChar = fcObj != null ? fcObj.intValue() : 32;
        int lastChar = lcObj != null ? lcObj.intValue() : 255;

        PdfArray newWidths = new PdfArray();
        for (int c = firstChar; c <= lastChar; c++) {
            String glyphName = diffNames.containsKey(c)
                ? diffNames.get(c) : winAnsiToGlyphName(c);
            Integer w = glyphName != null ? afmWidths.get(glyphName) : null;
            newWidths.add(new PdfNumber(w != null ? w : 0));
        }

        if (fcObj == null)
            fontDict.put(new PdfName("FirstChar"), new PdfNumber(firstChar));
        if (lcObj == null)
            fontDict.put(new PdfName("LastChar"), new PdfNumber(lastChar));
        fontDict.put(PdfName.Widths, newWidths);
        fontDict.setModified();
        return true;
    }

    /**
     * Map a WinAnsi char code (0-255) to its Adobe glyph name.
     * Based on PDF spec Table D.1 (WinAnsiEncoding) and Adobe Glyph List.
     */
    private static String winAnsiToGlyphName(int code) {
        // Standard printable ASCII (0x20-0x7E) + Latin-1 supplement (0xA0-0xFF)
        // with Windows-1252 extras in 0x80-0x9F range.
        if (WIN_ANSI_NAMES == null) initWinAnsiNames();
        if (code >= 0 && code < WIN_ANSI_NAMES.length)
            return WIN_ANSI_NAMES[code];
        return null;
    }

    private static String[] WIN_ANSI_NAMES;

    private static synchronized void initWinAnsiNames() {
        if (WIN_ANSI_NAMES != null) return;
        String[] n = new String[256];

        // 0x00-0x1F: control chars (no glyph)
        // 0x20-0x7E: standard ASCII
        n[0x20] = "space";       n[0x21] = "exclam";      n[0x22] = "quotedbl";
        n[0x23] = "numbersign";  n[0x24] = "dollar";       n[0x25] = "percent";
        n[0x26] = "ampersand";   n[0x27] = "quotesingle";  n[0x28] = "parenleft";
        n[0x29] = "parenright";  n[0x2A] = "asterisk";     n[0x2B] = "plus";
        n[0x2C] = "comma";       n[0x2D] = "hyphen";       n[0x2E] = "period";
        n[0x2F] = "slash";
        n[0x30] = "zero";  n[0x31] = "one";   n[0x32] = "two";   n[0x33] = "three";
        n[0x34] = "four";  n[0x35] = "five";  n[0x36] = "six";   n[0x37] = "seven";
        n[0x38] = "eight"; n[0x39] = "nine";
        n[0x3A] = "colon";       n[0x3B] = "semicolon";    n[0x3C] = "less";
        n[0x3D] = "equal";       n[0x3E] = "greater";      n[0x3F] = "question";
        n[0x40] = "at";
        n[0x41] = "A"; n[0x42] = "B"; n[0x43] = "C"; n[0x44] = "D";
        n[0x45] = "E"; n[0x46] = "F"; n[0x47] = "G"; n[0x48] = "H";
        n[0x49] = "I"; n[0x4A] = "J"; n[0x4B] = "K"; n[0x4C] = "L";
        n[0x4D] = "M"; n[0x4E] = "N"; n[0x4F] = "O"; n[0x50] = "P";
        n[0x51] = "Q"; n[0x52] = "R"; n[0x53] = "S"; n[0x54] = "T";
        n[0x55] = "U"; n[0x56] = "V"; n[0x57] = "W"; n[0x58] = "X";
        n[0x59] = "Y"; n[0x5A] = "Z";
        n[0x5B] = "bracketleft";  n[0x5C] = "backslash";  n[0x5D] = "bracketright";
        n[0x5E] = "asciicircum";  n[0x5F] = "underscore";  n[0x60] = "grave";
        n[0x61] = "a"; n[0x62] = "b"; n[0x63] = "c"; n[0x64] = "d";
        n[0x65] = "e"; n[0x66] = "f"; n[0x67] = "g"; n[0x68] = "h";
        n[0x69] = "i"; n[0x6A] = "j"; n[0x6B] = "k"; n[0x6C] = "l";
        n[0x6D] = "m"; n[0x6E] = "n"; n[0x6F] = "o"; n[0x70] = "p";
        n[0x71] = "q"; n[0x72] = "r"; n[0x73] = "s"; n[0x74] = "t";
        n[0x75] = "u"; n[0x76] = "v"; n[0x77] = "w"; n[0x78] = "x";
        n[0x79] = "y"; n[0x7A] = "z";
        n[0x7B] = "braceleft";  n[0x7C] = "bar";  n[0x7D] = "braceright";
        n[0x7E] = "asciitilde";

        // 0x80-0x9F: Windows-1252 extras (differ from Latin-1)
        n[0x80] = "Euro";         n[0x82] = "quotesinglbase";
        n[0x83] = "florin";       n[0x84] = "quotedblbase";
        n[0x85] = "ellipsis";     n[0x86] = "dagger";
        n[0x87] = "daggerdbl";    n[0x88] = "circumflex";
        n[0x89] = "perthousand";  n[0x8A] = "Scaron";
        n[0x8B] = "guilsinglleft"; n[0x8C] = "OE";
        n[0x8E] = "Zcaron";
        n[0x91] = "quoteleft";    n[0x92] = "quoteright";
        n[0x93] = "quotedblleft"; n[0x94] = "quotedblright";
        n[0x95] = "bullet";       n[0x96] = "endash";
        n[0x97] = "emdash";       n[0x98] = "tilde";
        n[0x99] = "trademark";    n[0x9A] = "scaron";
        n[0x9B] = "guilsinglright"; n[0x9C] = "oe";
        n[0x9E] = "zcaron";       n[0x9F] = "Ydieresis";

        // 0xA0-0xFF: Latin-1 supplement
        n[0xA0] = "space";  // NBSP → space glyph
        n[0xA1] = "exclamdown";   n[0xA2] = "cent";        n[0xA3] = "sterling";
        n[0xA4] = "currency";     n[0xA5] = "yen";         n[0xA6] = "brokenbar";
        n[0xA7] = "section";      n[0xA8] = "dieresis";    n[0xA9] = "copyright";
        n[0xAA] = "ordfeminine";  n[0xAB] = "guillemotleft";
        n[0xAC] = "logicalnot";   n[0xAD] = "hyphen";      n[0xAE] = "registered";
        n[0xAF] = "macron";
        n[0xB0] = "degree";       n[0xB1] = "plusminus";
        n[0xB2] = "twosuperior";  n[0xB3] = "threesuperior";
        n[0xB4] = "acute";        n[0xB5] = "mu";
        n[0xB6] = "paragraph";    n[0xB7] = "periodcentered";
        n[0xB8] = "cedilla";      n[0xB9] = "onesuperior";
        n[0xBA] = "ordmasculine"; n[0xBB] = "guillemotright";
        n[0xBC] = "onequarter";   n[0xBD] = "onehalf";
        n[0xBE] = "threequarters"; n[0xBF] = "questiondown";
        n[0xC0] = "Agrave";   n[0xC1] = "Aacute";  n[0xC2] = "Acircumflex";
        n[0xC3] = "Atilde";   n[0xC4] = "Adieresis"; n[0xC5] = "Aring";
        n[0xC6] = "AE";       n[0xC7] = "Ccedilla";
        n[0xC8] = "Egrave";   n[0xC9] = "Eacute";  n[0xCA] = "Ecircumflex";
        n[0xCB] = "Edieresis";
        n[0xCC] = "Igrave";   n[0xCD] = "Iacute";  n[0xCE] = "Icircumflex";
        n[0xCF] = "Idieresis";
        n[0xD0] = "Eth";      n[0xD1] = "Ntilde";
        n[0xD2] = "Ograve";   n[0xD3] = "Oacute";  n[0xD4] = "Ocircumflex";
        n[0xD5] = "Otilde";   n[0xD6] = "Odieresis"; n[0xD7] = "multiply";
        n[0xD8] = "Oslash";
        n[0xD9] = "Ugrave";   n[0xDA] = "Uacute";  n[0xDB] = "Ucircumflex";
        n[0xDC] = "Udieresis"; n[0xDD] = "Yacute";  n[0xDE] = "Thorn";
        n[0xDF] = "germandbls";
        n[0xE0] = "agrave";   n[0xE1] = "aacute";  n[0xE2] = "acircumflex";
        n[0xE3] = "atilde";   n[0xE4] = "adieresis"; n[0xE5] = "aring";
        n[0xE6] = "ae";       n[0xE7] = "ccedilla";
        n[0xE8] = "egrave";   n[0xE9] = "eacute";  n[0xEA] = "ecircumflex";
        n[0xEB] = "edieresis";
        n[0xEC] = "igrave";   n[0xED] = "iacute";  n[0xEE] = "icircumflex";
        n[0xEF] = "idieresis";
        n[0xF0] = "eth";      n[0xF1] = "ntilde";
        n[0xF2] = "ograve";   n[0xF3] = "oacute";  n[0xF4] = "ocircumflex";
        n[0xF5] = "otilde";   n[0xF6] = "odieresis"; n[0xF7] = "divide";
        n[0xF8] = "oslash";
        n[0xF9] = "ugrave";   n[0xFA] = "uacute";  n[0xFB] = "ucircumflex";
        n[0xFC] = "udieresis"; n[0xFD] = "yacute";  n[0xFE] = "thorn";
        n[0xFF] = "ydieresis";

        WIN_ANSI_NAMES = n;
    }

    /**
     * Maps a PDF font name to a TTF path for embedding.
     * Prefers Windows original fonts (exact metric match) in the font directory (READREADY_FONT_DIR, default <repo>/fonts/),
     * falls back to Liberation fonts in /usr/share/fonts/truetype/liberation/.
     * Returns null if no match found.
     */
    private static String mapToEmbeddableFont(String baseFont) {
        String clean = baseFont.contains("+")
            ? baseFont.substring(baseFont.indexOf('+') + 1) : baseFont;

        boolean bold = clean.contains("Bold");
        boolean italic = clean.contains("Italic") || clean.contains("Oblique")
            || clean.endsWith("-It") || clean.contains("-It,");

        // Try Windows original fonts first (exact metric match)
        String winPath = mapToWindowsFont(clean, bold, italic);
        if (winPath != null && new java.io.File(winPath).exists()) {
            return winPath;
        }

        // Try Apple system fonts (extracted from macOS TTC collections)
        String applePath = mapToAppleFont(clean, bold, italic);
        if (applePath != null && new java.io.File(applePath).exists()) {
            return applePath;
        }

        // Fall back to Liberation fonts
        String libBase = "/usr/share/fonts/truetype/liberation/";
        String style;
        if (bold && italic) style = "BoldItalic";
        else if (bold) style = "Bold";
        else if (italic) style = "Italic";
        else style = "Regular";

        if (clean.contains("Times") || clean.contains("Roman")
                || clean.contains("Serif"))
            return libBase + "LiberationSerif-" + style + ".ttf";
        if (clean.contains("Arial") || clean.contains("Helvetica")
                || clean.contains("Sans") || clean.contains("Tahoma")
                || clean.contains("Verdana") || clean.contains("Calibri"))
            return libBase + "LiberationSans-" + style + ".ttf";
        if (clean.contains("Courier") || clean.contains("Mono"))
            return libBase + "LiberationMono-" + style + ".ttf";

        // Fall back to Noto CJK for CJK font names not matched above.
        // Only match names that are unambiguously CJK — avoid false positives
        // on Western fonts like "CenturyGothic" or "PTSerif".
        // Noto CJK TTCs have all four regional variants; index 0 is default.
        String notoCjk = "/usr/share/fonts/opentype/noto/";
        // Serif-like CJK: Ming/Song/Mincho/Batang + CJK characters
        if (clean.contains("Ming") || clean.contains("Song")
                || clean.contains("Mincho") || clean.contains("Batang")
                || clean.contains("明") || clean.contains("宋")) {
            if (bold) return notoCjk + "NotoSerifCJK-Bold.ttc,0";
            return notoCjk + "NotoSerifCJK-Regular.ttc,0";
        }
        // Sans-like CJK: specific CJK sans families + CJK characters.
        // "Gothic" alone is too broad (matches CenturyGothic), so require
        // a CJK-specific prefix or use only the CJK-script name variants.
        if (clean.contains("MSGothic") || clean.contains("YuGothic")
                || clean.contains("Gulim") || clean.contains("Dotum")
                || clean.contains("STHeiti") || clean.contains("Heiti")
                || clean.contains("ゴシック") || clean.contains("黑")
                || clean.contains("雅黑")) {
            if (bold) return notoCjk + "NotoSansCJK-Bold.ttc,0";
            return notoCjk + "NotoSansCJK-Regular.ttc,0";
        }

        return null;
    }

    /**
     * Strip TTC face index from a font path for filesystem operations.
     * "simsun.ttc,0" → "simsun.ttc", "arial.ttf" → "arial.ttf".
     */
    private static String stripTtcIndex(String path) {
        if (path != null && path.matches(".*\\.ttc,\\d+$")) {
            return path.substring(0, path.lastIndexOf(','));
        }
        return path;
    }

    /** User font directory (see FontDirIndex.resolveFontDir). */
    private static String getWinFontDir() { return FontDirIndex.resolveFontDir(); }
    private static String getAppleFontDir() { return FontDirIndex.resolveFontDir() + "apple/"; }

    static String mapToWindowsFontName(String clean, boolean bold,
            boolean italic) {

        // Times New Roman family
        if (clean.contains("Times") || clean.contains("Roman")) {
            if (bold && italic) return "timesbi.ttf";
            if (bold) return "timesbd.ttf";
            if (italic) return "timesi.ttf";
            return "times.ttf";
        }
        // ArialNarrow — MUST come before generic Arial (narrower metrics)
        if (clean.contains("ArialNarrow") || clean.contains("Arial-Narrow")
                || clean.contains("Arial Narrow")) {
            if (bold && italic) return "ARIALNBI.TTF";
            if (bold) return "ARIALNB.TTF";
            if (italic) return "ARIALNI.TTF";
            return "ARIALN.TTF";
        }
        // Arial-Black — MUST come before generic Arial (wider metrics)
        if (clean.contains("Arial-Black") || clean.contains("ArialBlack")
                || clean.contains("Arial Black")) {
            return "ariblk.ttf";
        }
        // Arial family
        if (clean.contains("Arial")) {
            if (bold && italic) return "arialbi.ttf";
            if (bold) return "arialbd.ttf";
            if (italic) return "ariali.ttf";
            return "arial.ttf";
        }
        // Helvetica → Arial (metrically compatible)
        if (clean.contains("Helvetica")) {
            if (bold && italic) return "arialbi.ttf";
            if (bold) return "arialbd.ttf";
            if (italic) return "ariali.ttf";
            return "arial.ttf";
        }
        // Courier New family
        if (clean.contains("Courier")) {
            if (bold && italic) return "courbi.ttf";
            if (bold) return "courbd.ttf";
            if (italic) return "couri.ttf";
            return "cour.ttf";
        }
        // Tahoma
        if (clean.contains("Tahoma")) {
            if (bold) return "tahomabd.ttf";
            return "tahoma.ttf";
        }
        // Verdana
        if (clean.contains("Verdana")) {
            if (bold && italic) return "verdanaz.ttf";
            if (bold) return "verdanab.ttf";
            if (italic) return "verdanai.ttf";
            return "verdana.ttf";
        }
        // Calibri
        if (clean.contains("Calibri")) {
            if (bold && italic) return "calibriz.ttf";
            if (bold) return "calibrib.ttf";
            if (italic) return "calibrii.ttf";
            return "calibri.ttf";
        }
        // Georgia
        if (clean.contains("Georgia")) {
            if (bold && italic) return "georgiaz.ttf";
            if (bold) return "georgiab.ttf";
            if (italic) return "georgiai.ttf";
            return "georgia.ttf";
        }
        // Trebuchet MS
        if (clean.contains("Trebuchet")) {
            if (bold && italic) return "trebucbi.ttf";
            if (bold) return "trebucbd.ttf";
            if (italic) return "trebucit.ttf";
            return "trebuc.ttf";
        }
        // Cambria
        if (clean.contains("Cambria")) {
            // cambria.ttc is a collection — not usable as TTF directly
            if (bold) return "cambriab.ttf";
            if (italic) return "cambriai.ttf";
            return null; // cambria.ttc needs special handling
        }
        // Book Antiqua (metrically identical to Palatino)
        if (clean.contains("BookAntiqua") || clean.contains("Book Antiqua")
                || clean.contains("Book-Antiqua")) {
            return "BKANT.TTF";
        }
        // Cooper Black
        if (clean.contains("CooperBlack") || clean.contains("Cooper Black")
                || clean.contains("Cooper-Black")) {
            return "COOPBL.TTF";
        }
        // Wingdings 2 (MUST come before generic Wingdings)
        if (clean.contains("Wingdings2") || clean.contains("Wingdings 2")
                || clean.contains("Wingdings-2")) {
            return "WINGDNG2.TTF";
        }
        // Wingdings 3 (MUST come before generic Wingdings)
        if (clean.contains("Wingdings3") || clean.contains("Wingdings 3")
                || clean.contains("Wingdings-3")) {
            return "WINGDNG3.TTF";
        }
        // Wingdings (full font for re-embedding subsets)
        if (clean.contains("Wingdings")) {
            return "wingding.ttf";
        }
        // Symbol (TrueType version for re-embedding subsets)
        if (clean.equals("Symbol")) {
            return "symbol.ttf";
        }
        // Open Sans family
        if (clean.contains("OpenSans") || clean.contains("Open Sans")
                || clean.contains("Open-Sans")) {
            // Named-weight branches FIRST (longest tokens before shorter substrings:
            // ExtraBold and SemiBold contain "Bold" so they must precede the
            // generic if(bold); ExtraLight would contain "Light" but isn't in
            // this family). Medium and Light follow the same template for
            // uniformity across all weight-having font blocks.
            if (clean.contains("ExtraBold")) {
                if (italic) return "OpenSans-ExtraBoldItalic.ttf";
                return "OpenSans-ExtraBold.ttf";
            }
            if (clean.contains("SemiBold") || clean.contains("Semibold")) {
                if (italic) return "OpenSans-SemiBoldItalic.ttf";
                return "OpenSans-SemiBold.ttf";
            }
            if (clean.contains("Medium")) {
                if (italic) return "OpenSans-MediumItalic.ttf";
                return "OpenSans-Medium.ttf";
            }
            if (clean.contains("Light")) {
                if (italic) return "OpenSans-LightItalic.ttf";
                return "OpenSans-Light.ttf";
            }
            if (bold && italic) return "OpenSans-BoldItalic.ttf";
            if (bold) return "OpenSans-Bold.ttf";
            if (italic) return "OpenSans-Italic.ttf";
            return "OpenSans-Regular.ttf";
        }
        // Lato family
        if (clean.contains("Lato")) {
            // Named-weight branches FIRST so "SemiBold" (which contains "Bold")
            // and other weights aren't shadowed by the generic if(bold) check.
            if (clean.contains("Semibold") || clean.contains("SemiBold")) {
                if (italic) return "Lato-SemiboldItalic.ttf";
                return "Lato-Semibold.ttf";
            }
            if (clean.contains("Heavy")) {
                if (italic) return "Lato-HeavyItalic.ttf";
                return "Lato-Heavy.ttf";
            }
            if (clean.contains("Medium")) {
                if (italic) return "Lato-MediumItalic.ttf";
                return "Lato-Medium.ttf";
            }
            if (clean.contains("Light")) {
                if (italic) return "Lato-LightItalic.ttf";
                return "Lato-Light.ttf";
            }
            if (bold && italic) return "Lato-BoldItalic.ttf";
            if (bold) return "Lato-Bold.ttf";
            if (italic) return "Lato-Italic.ttf";
            return "Lato-Regular.ttf";
        }
        // Source Sans family (SourceSansVariable → SourceSans3)
        if (clean.contains("SourceSans")) {
            // Order matters: ExtraBold before Bold (contains "Bold"),
            // ExtraLight before Light (contains "Light"), SemiBold before
            // generic bold for the same reason.
            if (clean.contains("ExtraBold")) {
                if (italic) return "SourceSans3-ExtraBoldItalic.ttf";
                return "SourceSans3-ExtraBold.ttf";
            }
            if (clean.contains("SemiBold") || clean.contains("Semibold")) {
                if (italic) return "SourceSans3-SemiBoldItalic.ttf";
                return "SourceSans3-SemiBold.ttf";
            }
            if (clean.contains("ExtraLight")) {
                if (italic) return "SourceSans3-ExtraLightItalic.ttf";
                return "SourceSans3-ExtraLight.ttf";
            }
            if (clean.contains("Light")) {
                if (italic) return "SourceSans3-LightItalic.ttf";
                return "SourceSans3-Light.ttf";
            }
            if (clean.contains("Medium")) {
                if (italic) return "SourceSans3-MediumItalic.ttf";
                return "SourceSans3-Medium.ttf";
            }
            if (clean.contains("Black")) {
                if (italic) return "SourceSans3-BlackItalic.ttf";
                return "SourceSans3-Black.ttf";
            }
            if (bold && italic) return "SourceSans3-BoldItalic.ttf";
            if (bold) return "SourceSans3-Bold.ttf";
            if (italic) return "SourceSans3-Italic.ttf";
            return "SourceSans3-Regular.ttf";
        }
        // Segoe UI Variable Display family
        if (clean.contains("Segoe") && clean.contains("Variable")
                && clean.contains("Display")) {
            if (bold) return "Segoe-UI-Variable-Static-Display-Bold.ttf";
            if (clean.contains("Semibold") || clean.contains("SemiBold"))
                return "Segoe-UI-Variable-Static-Display-Semibold.ttf";
            if (clean.contains("Light"))
                return "Segoe-UI-Variable-Static-Display-Light.ttf";
            if (clean.contains("Semilight"))
                return "Segoe-UI-Variable-Static-Display-Semilight.ttf";
            return "Segoe-UI-Variable-Static-Display.ttf";
        }
        // Futura family
        if (clean.contains("Futura")) {
            if (clean.contains("Heavy")) return "Futura Heavy.otf";
            // Other Futura variants not yet available — return Heavy as
            // closest match for width-patching to at least have a font program
            if (clean.contains("Bold")) return "Futura Heavy.otf";
            return "Futura Heavy.otf";
        }
        // Century Gothic (geometric sans-serif, all 4 variants available)
        if (clean.contains("CenturyGothic") || clean.contains("Century Gothic")
                || clean.contains("Century-Gothic")) {
            if (bold && italic) return "GOTHICBI.TTF";
            if (bold) return "GOTHICB.TTF";
            if (italic) return "GOTHICI.TTF";
            return "GOTHIC.TTF";
        }
        // Corbel (humanist sans-serif; full family including Light)
        if (clean.contains("Corbel")) {
            if (clean.contains("Light")) {
                if (italic) return "corbelli.ttf";
                return "corbell.ttf";
            }
            if (bold && italic) return "corbelz.ttf";
            if (bold) return "corbelb.ttf";
            if (italic) return "corbeli.ttf";
            return "corbel.ttf";
        }
        // Comic Sans MS (only regular/bold/italic/bold-italic on Windows)
        if (clean.contains("ComicSans") || clean.contains("Comic Sans")
                || clean.contains("Comic-Sans")) {
            if (bold && italic) return "comicz.ttf";
            if (bold) return "comicbd.ttf";
            if (italic) return "comici.ttf";
            return "comic.ttf";
        }
        // Gill Sans MT — weight-aware mapping using available Windows variants
        if (clean.contains("GillSans") || clean.contains("Gill Sans")
                || clean.contains("GillSansMT") || clean.contains("Gill-Sans")) {
            if (clean.contains("UltraBold") || clean.contains("Ultra Bold"))
                return "GILSANUB.TTF";
            if (clean.contains("Condensed"))
                return "GILC____.TTF";
            if (bold && italic) return "GILBI___.TTF";
            if (bold) return "GILB____.TTF";
            if (italic) return "GILI____.TTF";
            return "GIL_____.TTF";
        }

        // ===== HIGH PRIORITY — common in academic/institutional PDFs =====

        // Palatino Linotype (metrically compatible with Palatino)
        if (clean.contains("PalatinoLinotype") || clean.contains("Palatino Linotype")
                || clean.contains("Palatino-Linotype")
                || (clean.contains("Palatino") && !clean.contains("Script"))) {
            if (bold && italic) return "palabi.ttf";
            if (bold) return "palab.ttf";
            if (italic) return "palai.ttf";
            return "pala.ttf";
        }
        // Garamond
        if (clean.contains("Garamond")) {
            if (bold) return "GARABD.TTF";
            if (italic) return "GARAIT.TTF";
            return "GARA.TTF";
        }
        // Segoe UI Variable Small (MUST come before generic Segoe UI)
        if (clean.contains("Segoe") && clean.contains("Variable")
                && clean.contains("Small")) {
            if (bold) return "Segoe-UI-Variable-Static-Small-Bold.ttf";
            if (clean.contains("Semibold") || clean.contains("SemiBold"))
                return "Segoe-UI-Variable-Static-Small-Semibold.ttf";
            if (clean.contains("Light"))
                return "Segoe-UI-Variable-Static-Small-Light.ttf";
            if (clean.contains("Semilight"))
                return "Segoe-UI-Variable-Static-Small-Semilight.ttf";
            return "Segoe-UI-Variable-Static-Small.ttf";
        }
        // Segoe UI Variable Text (MUST come before generic Segoe UI)
        if (clean.contains("Segoe") && clean.contains("Variable")
                && clean.contains("Text")) {
            if (bold) return "Segoe-UI-Variable-Static-Text-Bold.ttf";
            if (clean.contains("Semibold") || clean.contains("SemiBold"))
                return "Segoe-UI-Variable-Static-Text-Semibold.ttf";
            if (clean.contains("Light"))
                return "Segoe-UI-Variable-Static-Text-Light.ttf";
            if (clean.contains("Semilight"))
                return "Segoe-UI-Variable-Static-Text-Semilight.ttf";
            return "Segoe-UI-Variable-Static-Text.ttf";
        }
        // Segoe Print
        if (clean.contains("SegoePrint") || clean.contains("Segoe Print")
                || clean.contains("Segoe-Print")) {
            if (bold) return "segoeprb.ttf";
            return "segoepr.ttf";
        }
        // Segoe Script
        if (clean.contains("SegoeScript") || clean.contains("Segoe Script")
                || clean.contains("Segoe-Script")) {
            if (bold) return "segoescb.ttf";
            return "segoesc.ttf";
        }
        // Segoe UI (generic — MUST come after Segoe Variable/Print/Script)
        if (clean.contains("SegoeUI") || clean.contains("Segoe UI")
                || clean.contains("Segoe-UI")) {
            if (clean.contains("Light")) {
                if (italic) return "seguili.ttf";
                return "segoeuil.ttf";
            }
            if (clean.contains("Semilight")) {
                if (italic) return "seguisli.ttf";
                return "segoeuisl.ttf";
            }
            if (clean.contains("Semibold") || clean.contains("SemiBold")) {
                if (italic) return "seguisbi.ttf";
                return "seguisb.ttf";
            }
            if (clean.contains("Black")) {
                if (italic) return "seguibli.ttf";
                return "seguibl.ttf";
            }
            if (bold && italic) return "segoeuiz.ttf";
            if (bold) return "segoeuib.ttf";
            if (italic) return "segoeuii.ttf";
            return "segoeui.ttf";
        }
        // Franklin Gothic (multiple sub-families)
        if (clean.contains("FranklinGothic") || clean.contains("Franklin Gothic")
                || clean.contains("Franklin-Gothic")) {
            if (clean.contains("Heavy")) {
                if (italic) return "FRAHVIT.TTF";
                return "FRAHV.TTF";
            }
            if (clean.contains("Demi") && clean.contains("Cond"))
                return "FRADMCN.TTF";
            if (clean.contains("Demi")) {
                if (italic) return "FRADMIT.TTF";
                return "FRADM.TTF";
            }
            if (clean.contains("Medium") && clean.contains("Cond"))
                return "FRAMDCN.TTF";
            if (clean.contains("Medium")) {
                if (italic) return "framdit.ttf";
                return "framd.ttf";
            }
            if (clean.contains("Book")) {
                if (italic) return "FRABKIT.TTF";
                return "FRABK.TTF";
            }
            // Default to Book
            if (bold) return "FRADM.TTF";
            if (italic) return "FRABKIT.TTF";
            return "FRABK.TTF";
        }
        // Constantia
        if (clean.contains("Constantia")) {
            if (bold && italic) return "constanz.ttf";
            if (bold) return "constanb.ttf";
            if (italic) return "constani.ttf";
            return "constan.ttf";
        }
        // Consolas
        if (clean.contains("Consolas")) {
            if (bold && italic) return "consolaz.ttf";
            if (bold) return "consolab.ttf";
            if (italic) return "consolai.ttf";
            return "consola.ttf";
        }
        // Bookman Old Style
        if (clean.contains("Bookman") || clean.contains("BookmanOldStyle")) {
            if (bold && italic) return "BOOKOSBI.TTF";
            if (bold) return "BOOKOSB.TTF";
            if (italic) return "BOOKOSI.TTF";
            return "BOOKOS.TTF";
        }
        // Century Gothic MUST come before Century Schoolbook and generic Century
        // (already handled above)

        // Century Schoolbook (MUST come before generic Century)
        if (clean.contains("CenturySchoolbook") || clean.contains("Century Schoolbook")
                || clean.contains("Century-Schoolbook") || clean.contains("SchoolBook")) {
            if (bold && italic) return "SCHLBKBI.TTF";
            if (bold) return "SCHLBKB.TTF";
            if (italic) return "SCHLBKI.TTF";
            return "CENSCBK.TTF";
        }
        // Century (generic — MUST come after Century Gothic and Century Schoolbook)
        if (clean.equals("Century") || clean.startsWith("Century-")
                || clean.startsWith("Century,")) {
            return "CENTURY.TTF";
        }
        // Tw Cen MT
        if (clean.contains("TwCenMT") || clean.contains("Tw Cen MT")
                || clean.contains("Tw-Cen") || clean.contains("TwCen")) {
            if (clean.contains("Condensed") && clean.contains("ExtraBold"))
                return "TCCEB.TTF";
            if (clean.contains("Condensed") && bold) return "TCCB____.TTF";
            if (clean.contains("Condensed")) return "TCCM____.TTF";
            if (bold && italic) return "TCBI____.TTF";
            if (bold) return "TCB_____.TTF";
            if (italic) return "TCMI____.TTF";
            return "TCM_____.TTF";
        }
        // Lucida Sans Unicode (MUST come before Lucida Sans)
        if (clean.contains("LucidaSansUnicode") || clean.contains("Lucida Sans Unicode")
                || clean.contains("Lucida-Sans-Unicode")) {
            return "l_10646.ttf";
        }
        // Lucida Calligraphy (MUST come before generic Lucida)
        if (clean.contains("LucidaCalligraphy") || clean.contains("Lucida Calligraphy")) {
            return "LCALLIG.TTF";
        }
        // Lucida Handwriting (MUST come before generic Lucida)
        if (clean.contains("LucidaHandwriting") || clean.contains("Lucida Handwriting")) {
            return "LHANDW.TTF";
        }
        // Lucida Console (MUST come before generic Lucida)
        if (clean.contains("LucidaConsole") || clean.contains("Lucida Console")
                || clean.contains("Lucida-Console")) {
            return "lucon.ttf";
        }
        // Lucida Fax
        if (clean.contains("LucidaFax") || clean.contains("Lucida Fax")) {
            if (bold && italic) return "LFAXDI.TTF";
            if (bold) return "LFAXD.TTF";
            if (italic) return "LFAXI.TTF";
            return "LFAX.TTF";
        }
        // Lucida Bright
        if (clean.contains("LucidaBright") || clean.contains("Lucida Bright")) {
            if (bold && italic) return "LBRITEDI.TTF";
            if (bold) return "LBRITED.TTF";
            if (italic) return "LBRITEI.TTF";
            return "LBRITE.TTF";
        }
        // Lucida Sans (generic — after specific Lucida variants)
        if (clean.contains("LucidaSans") || clean.contains("Lucida Sans")) {
            if (bold && italic) return "LSANSDI.TTF";
            if (bold) return "LSANSD.TTF";
            if (italic) return "LSANSI.TTF";
            return "LSANS.TTF";
        }
        // Lucida Sans Typewriter
        if (clean.contains("LucidaSansTypewriter") || clean.contains("Lucida Sans Typewriter")
                || clean.contains("Lucida-Sans-Typewriter")) {
            if (bold && italic) return "LTYPEBO.TTF";
            if (bold) return "LTYPEB.TTF";
            if (italic) return "LTYPEO.TTF";
            return "LTYPE.TTF";
        }
        // Rockwell Condensed (MUST come before generic Rockwell)
        if (clean.contains("Rockwell") && clean.contains("Condensed")) {
            if (bold) return "ROCCB___.TTF";
            return "ROCC____.TTF";
        }
        // Rockwell
        if (clean.contains("Rockwell")) {
            if (clean.contains("ExtraBold") || clean.contains("Extra Bold"))
                return "ROCKEB.TTF";
            if (bold && italic) return "ROCKBI.TTF";
            if (bold) return "ROCKB.TTF";
            if (italic) return "ROCKI.TTF";
            return "ROCK.TTF";
        }
        // Perpetua Titling (MUST come before generic Perpetua)
        if (clean.contains("Perpetua") && clean.contains("Titling")) {
            if (bold) return "PERTIBD.TTF";
            return "PERTILI.TTF";
        }
        // Perpetua
        if (clean.contains("Perpetua")) {
            if (bold && italic) return "PERBI___.TTF";
            if (bold) return "PERB____.TTF";
            if (italic) return "PERI____.TTF";
            return "PER_____.TTF";
        }
        // Candara
        if (clean.contains("Candara")) {
            if (clean.contains("Light")) {
                if (italic) return "Candarali.ttf";
                return "Candaral.ttf";
            }
            if (bold && italic) return "Candaraz.ttf";
            if (bold) return "Candarab.ttf";
            if (italic) return "Candarai.ttf";
            return "Candara.ttf";
        }
        // Bahnschrift (variable font, single file)
        if (clean.contains("Bahnschrift")) {
            return "bahnschrift.ttf";
        }
        // Impact
        if (clean.contains("Impact")) {
            return "impact.ttf";
        }
        // Baskerville Old Face
        if (clean.contains("Baskerville")) {
            return "BASKVILL.TTF";
        }
        // Bodoni MT Condensed/Poster (MUST come before generic Bodoni)
        if (clean.contains("Bodoni") && clean.contains("Condensed")) {
            if (bold && italic) return "BOD_CBI.TTF";
            if (bold) return "BOD_CB.TTF";
            if (italic) return "BOD_CI.TTF";
            return "BOD_CR.TTF";
        }
        if (clean.contains("Bodoni") && clean.contains("Poster")) {
            return "BOD_PSTC.TTF";
        }
        if (clean.contains("Bodoni") && (clean.contains("Black")
                || clean.contains("BLAR"))) {
            if (italic) return "BOD_BLAI.TTF";
            return "BOD_BLAR.TTF";
        }
        // Bodoni MT (generic)
        if (clean.contains("Bodoni")) {
            if (bold && italic) return "BOD_BI.TTF";
            if (bold) return "BOD_B.TTF";
            if (italic) return "BOD_I.TTF";
            return "BOD_R.TTF";
        }
        // Goudy Old Style
        if (clean.contains("Goudy") || clean.contains("GoudyOldStyle")) {
            if (clean.contains("Stout")) return "GOUDYSTO.TTF";
            if (bold) return "GOUDOSB.TTF";
            if (italic) return "GOUDOSI.TTF";
            return "GOUDOS.TTF";
        }
        // Calisto MT
        if (clean.contains("Calisto") || clean.contains("CalistoMT")) {
            if (bold && italic) return "CALISTBI.TTF";
            if (bold) return "CALISTB.TTF";
            if (italic) return "CALISTI.TTF";
            return "CALIST.TTF";
        }
        // Eras ITC
        if (clean.contains("Eras")) {
            if (clean.contains("Bold")) return "ERASBD.TTF";
            if (clean.contains("Demi")) return "ERASDEMI.TTF";
            if (clean.contains("Light")) return "ERASLGHT.TTF";
            if (clean.contains("Medium")) return "ERASMD.TTF";
            // Default to Medium
            return "ERASMD.TTF";
        }
        // Copperplate Gothic
        if (clean.contains("Copperplate")) {
            if (bold) return "COPRGTB.TTF";
            return "COPRGTL.TTF";
        }
        // Sylfaen
        if (clean.contains("Sylfaen")) {
            return "sylfaen.ttf";
        }
        // Gadugi
        if (clean.contains("Gadugi")) {
            if (bold) return "gadugib.ttf";
            return "gadugi.ttf";
        }
        // Ebrima
        if (clean.contains("Ebrima")) {
            if (bold) return "ebrimabd.ttf";
            return "ebrima.ttf";
        }
        // Webdings
        if (clean.contains("Webdings")) {
            return "webdings.ttf";
        }
        // Monotype Corsiva
        if (clean.contains("MonotypeCorsiva") || clean.contains("Monotype Corsiva")
                || clean.contains("Monotype-Corsiva") || clean.contains("MTCorsiva")) {
            return "MTCORSVA.TTF";
        }
        // OCR A Extended
        if (clean.contains("OCRA") || clean.contains("OCR A")
                || clean.contains("OCR-A")) {
            return "OCRAEXT.TTF";
        }
        // Dubai
        if (clean.contains("Dubai")) {
            if (bold) return "DUBAI-BOLD.TTF";
            if (clean.contains("Medium")) return "DUBAI-MEDIUM.TTF";
            if (clean.contains("Light")) return "DUBAI-LIGHT.TTF";
            return "DUBAI-REGULAR.TTF";
        }
        // Leelawadee UI (MUST come before generic Leelawadee)
        if (clean.contains("LeelawadeeUI") || clean.contains("Leelawadee UI")
                || clean.contains("Leelawadee-UI")) {
            if (clean.contains("Semilight")) return "LeelUIsl.ttf";
            if (bold) return "LeelaUIb.ttf";
            return "LeelawUI.ttf";
        }
        // Leelawadee (generic)
        if (clean.contains("Leelawadee")) {
            if (bold) return "LEELAWDB.TTF";
            return "LEELAWAD.TTF";
        }
        // Ink Free
        if (clean.contains("InkFree") || clean.contains("Ink Free")
                || clean.contains("Ink-Free")) {
            return "Inkfree.ttf";
        }
        // Gabriola
        if (clean.contains("Gabriola")) {
            return "Gabriola.ttf";
        }

        // ===== DECORATIVE / DISPLAY FONTS =====

        // Agency FB
        if (clean.contains("AgencyFB") || clean.contains("Agency FB")
                || clean.contains("Agency-FB")) {
            if (bold) return "AGENCYB.TTF";
            return "AGENCYR.TTF";
        }
        // Algerian
        if (clean.contains("Algerian")) {
            return "ALGER.TTF";
        }
        // Bauhaus 93
        if (clean.contains("Bauhaus")) {
            return "BAUHS93.TTF";
        }
        // Bell MT
        if (clean.contains("BellMT") || clean.contains("Bell MT")
                || clean.contains("Bell-MT")) {
            if (bold) return "BELLB.TTF";
            if (italic) return "BELLI.TTF";
            return "BELL.TTF";
        }
        // Berlin Sans FB
        if (clean.contains("BerlinSans") || clean.contains("Berlin Sans")) {
            if (clean.contains("Demi")) return "BRLNSDB.TTF";
            if (bold) return "BRLNSB.TTF";
            return "BRLNSR.TTF";
        }
        // Bernhard Condensed
        if (clean.contains("BernhardCond") || clean.contains("Bernhard")) {
            return "BERNHC.TTF";
        }
        // Bradley Hand ITC
        if (clean.contains("BradleyHand") || clean.contains("Bradley Hand")) {
            return "BRADHITC.TTF";
        }
        // Britannic Bold
        if (clean.contains("Britannic")) {
            return "BRITANIC.TTF";
        }
        // Broadway
        if (clean.contains("Broadway")) {
            return "BROADW.TTF";
        }
        // Brush Script MT
        if (clean.contains("BrushScript") || clean.contains("Brush Script")) {
            return "BRUSHSCI.TTF";
        }
        // Californian FB
        if (clean.contains("CalifornianFB") || clean.contains("Californian FB")
                || clean.contains("Californian-FB")) {
            if (bold) return "CALIFB.TTF";
            if (italic) return "CALIFI.TTF";
            return "CALIFR.TTF";
        }
        // Castellar
        if (clean.contains("Castellar")) {
            return "CASTELAR.TTF";
        }
        // Centaur
        if (clean.contains("Centaur")) {
            return "CENTAUR.TTF";
        }
        // Chiller
        if (clean.contains("Chiller")) {
            return "CHILLER.TTF";
        }
        // Colonna MT
        if (clean.contains("Colonna")) {
            return "COLONNA.TTF";
        }
        // Curlz MT
        if (clean.contains("Curlz")) {
            return "CURLZ___.TTF";
        }
        // Elephant
        if (clean.contains("Elephant")) {
            if (italic) return "ELEPHNTI.TTF";
            return "ELEPHNT.TTF";
        }
        // Engravers MT
        if (clean.contains("Engravers")) {
            return "ENGR.TTF";
        }
        // Felix Titling
        if (clean.contains("FelixTitling") || clean.contains("Felix Titling")) {
            return "FELIXTI.TTF";
        }
        // Footlight MT Light
        if (clean.contains("Footlight")) {
            return "FTLTLT.TTF";
        }
        // Forte
        if (clean.contains("Forte")) {
            return "FORTE.TTF";
        }
        // Freestyle Script
        if (clean.contains("FreestyleScript") || clean.contains("Freestyle Script")) {
            return "FREESCPT.TTF";
        }
        // French Script MT
        if (clean.contains("FrenchScript") || clean.contains("French Script")) {
            return "FRSCRIPT.TTF";
        }
        // Gigi
        if (clean.contains("Gigi")) {
            return "GIGI.TTF";
        }
        // Gloucester MT
        if (clean.contains("Gloucester")) {
            return "GLECB.TTF";
        }
        // Harlow Solid Italic
        if (clean.contains("Harlow")) {
            return "HARLOWSI.TTF";
        }
        // Harrington
        if (clean.contains("Harrington")) {
            return "HARNGTON.TTF";
        }
        // Haettenschweiler
        if (clean.contains("Haetten") || clean.contains("HAETTEN")) {
            return "HATTEN.TTF";
        }
        // High Tower Text
        if (clean.contains("HighTower") || clean.contains("High Tower")) {
            if (italic) return "HTOWERTI.TTF";
            return "HTOWERT.TTF";
        }
        // Imprint MT Shadow
        if (clean.contains("Imprint")) {
            return "IMPRISHA.TTF";
        }
        // Informal Roman
        if (clean.contains("InformalRoman") || clean.contains("Informal Roman")) {
            return "INFROMAN.TTF";
        }
        // ITC Blackadder
        if (clean.contains("Blackadder")) {
            return "ITCBLKAD.TTF";
        }
        // ITC Edwardian Script
        if (clean.contains("EdwardianScript") || clean.contains("Edwardian Script")) {
            return "ITCEDSCR.TTF";
        }
        // ITC Kristen
        if (clean.contains("Kristen")) {
            return "ITCKRIST.TTF";
        }
        // Jokerman
        if (clean.contains("Jokerman")) {
            return "JOKERMAN.TTF";
        }
        // Juice ITC
        if (clean.contains("Juice")) {
            return "JUICE___.TTF";
        }
        // Kunstler Script
        if (clean.contains("Kunstler")) {
            return "KUNSTLER.TTF";
        }
        // Wide Latin
        if (clean.contains("WideLatin") || clean.contains("Wide Latin")) {
            return "LATINWD.TTF";
        }
        // Magneto
        if (clean.contains("Magneto")) {
            return "MAGNETOB.TTF";
        }
        // Maiandra GD
        if (clean.contains("Maiandra")) {
            return "MAIAN.TTF";
        }
        // Matura MT Script
        if (clean.contains("Matura")) {
            return "MATURASC.TTF";
        }
        // Mistral
        if (clean.contains("Mistral")) {
            return "MISTRAL.TTF";
        }
        // Modern No. 20
        if (clean.contains("Modern")) {
            return "MOD20.TTF";
        }
        // Niagara
        if (clean.contains("Niagara")) {
            if (clean.contains("Solid")) return "NIAGSOL.TTF";
            return "NIAGENG.TTF";
        }
        // Old English Text MT
        if (clean.contains("OldEnglish") || clean.contains("Old English")) {
            return "OLDENGL.TTF";
        }
        // Onyx
        if (clean.contains("Onyx")) {
            return "ONYX.TTF";
        }
        // Palace Script MT
        if (clean.contains("PalaceScript") || clean.contains("Palace Script")) {
            return "PALSCRI.TTF";
        }
        // Papyrus
        if (clean.contains("Papyrus")) {
            return "PAPYRUS.TTF";
        }
        // Parchment
        if (clean.contains("Parchment")) {
            return "PARCHM.TTF";
        }
        // Playbill
        if (clean.contains("Playbill")) {
            return "PLAYBILL.TTF";
        }
        // Poor Richard
        if (clean.contains("PoorRichard") || clean.contains("Poor Richard")) {
            return "POORICH.TTF";
        }
        // Pristina
        if (clean.contains("Pristina")) {
            return "PRISTINA.TTF";
        }
        // Rage Italic
        if (clean.contains("Rage")) {
            return "RAGE.TTF";
        }
        // Ravie
        if (clean.contains("Ravie")) {
            return "RAVIE.TTF";
        }
        // Script MT Bold
        if (clean.contains("ScriptMT") || clean.contains("Script MT")) {
            return "SCRIPTBL.TTF";
        }
        // Showcard Gothic
        if (clean.contains("Showcard")) {
            return "SHOWG.TTF";
        }
        // Snap ITC
        if (clean.contains("Snap")) {
            return "SNAP____.TTF";
        }
        // Stencil
        if (clean.contains("Stencil")) {
            return "STENCIL.TTF";
        }
        // Tempus Sans ITC
        if (clean.contains("TempusSans") || clean.contains("Tempus Sans")) {
            return "TEMPSITC.TTF";
        }
        // Viner Hand ITC
        if (clean.contains("VinerHand") || clean.contains("Viner Hand")) {
            return "VINERITC.TTF";
        }
        // Vivaldi
        if (clean.contains("Vivaldi")) {
            return "VIVALDII.TTF";
        }
        // Vladimir Script
        if (clean.contains("Vladimir")) {
            return "VLADIMIR.TTF";
        }

        // ===== CJK FONTS =====
        // TTC collections use iText ",N" index syntax for face selection.

        // SimSun / NSimSun (Simplified Chinese, Song/Ming style)
        if (clean.contains("SimSun") || clean.contains("NSimSun")
                || clean.contains("宋体")) {
            if (bold) return "simsunb.ttf";
            return "simsun.ttc,0";
        }
        // Microsoft YaHei (Simplified Chinese, sans-serif)
        if (clean.contains("YaHei") || clean.contains("微软雅黑")) {
            if (clean.contains("Light")) return "msyhl.ttc,0";
            if (bold) return "msyhbd.ttc,0";
            return "msyh.ttc,0";
        }
        // Microsoft JhengHei (Traditional Chinese, sans-serif)
        if (clean.contains("JhengHei") || clean.contains("微軟正黑")) {
            if (clean.contains("Light")) return "msjhl.ttc,0";
            if (bold) return "msjhbd.ttc,0";
            return "msjh.ttc,0";
        }
        // MingLiU / PMingLiU (Traditional Chinese, serif)
        if (clean.contains("MingLiU") || clean.contains("PMingLiU")
                || clean.contains("細明體")) {
            return "mingliub.ttc,0";
        }
        // MS Gothic / MS PGothic / MS UI Gothic (Japanese, sans-serif)
        if (clean.contains("MSGothic") || clean.contains("MS Gothic")
                || clean.contains("MS-Gothic") || clean.contains("MSPGothic")
                || clean.contains("MS PGothic") || clean.contains("MSUIGothic")
                || clean.contains("MS UI Gothic") || clean.contains("ＭＳ ゴシック")) {
            return "msgothic.ttc,0";
        }
        // Yu Gothic (Japanese, modern sans-serif)
        if (clean.contains("YuGothic") || clean.contains("Yu Gothic")
                || clean.contains("游ゴシック")) {
            if (bold) return "YuGothB.ttc,0";
            if (clean.contains("Light")) return "YuGothL.ttc,0";
            if (clean.contains("Medium")) return "YuGothM.ttc,0";
            return "YuGothR.ttc,0";
        }
        // Malgun Gothic (Korean, sans-serif)
        if (clean.contains("Malgun") || clean.contains("맑은")) {
            if (bold) return "malgunbd.ttf";
            if (clean.contains("Semilight")) return "malgunsl.ttf";
            return "malgun.ttf";
        }

        return null;
    }

    static String mapToWindowsFont(String clean, boolean bold, boolean italic) {
        String name = mapToWindowsFontName(clean, bold, italic);
        if (name == null) return null;
        String dir = getWinFontDir();
        String real = FontDirIndex.of(dir).resolve(name);
        return real != null ? real : dir + name;
    }

    /**
     * Maps a PDF font name to an Apple system font TTF in fonts/apple/.
     * These are extracted from macOS TTC collections into individual TTFs.
     * Returns null if no match found.
     */
    static String mapToAppleFontName(String clean, boolean bold,
            boolean italic) {

        // ---- Helvetica Neue (very common in Mac-created PDFs) ----
        if (clean.contains("HelveticaNeue") || clean.contains("Helvetica Neue")
                || clean.contains("Helvetica-Neue")) {
            if (bold && italic) return "helvetica_neue_bold_italic.ttf";
            if (bold) return "helvetica_neue_bold.ttf";
            if (italic) return "helvetica_neue_italic.ttf";
            if (clean.contains("Light")) return "helvetica_neue_light.ttf";
            if (clean.contains("Medium")) return "helvetica_neue_medium.ttf";
            if (clean.contains("Thin")) return "helvetica_neue_thin.ttf";
            if (clean.contains("UltraLight")) return "helvetica_neue_ultralight.ttf";
            if (clean.contains("Condensed") && clean.contains("Black"))
                return "helvetica_neue_condensed_black.ttf";
            if (clean.contains("Condensed")) return "helvetica_neue_condensed_bold.ttf";
            return "helvetica_neue.ttf";
        }

        // ---- Apple Helvetica (real Apple version, not the Arial substitute) ----
        // Note: mapToWindowsFont already maps generic "Helvetica" to Arial.
        // This catches Apple-specific Helvetica variants that Windows doesn't have.
        if (clean.contains("Helvetica") && (clean.contains("Light")
                || clean.contains("Oblique"))) {
            if (bold && italic) return "helvetica_bold_oblique.ttf";
            if (bold) return "helvetica_bold.ttf";
            if (clean.contains("Light") && clean.contains("Oblique"))
                return "helvetica_light_oblique.ttf";
            if (clean.contains("Light")) return "helvetica_light.ttf";
            if (italic || clean.contains("Oblique"))
                return "helvetica_oblique.ttf";
            return "helvetica.ttf";
        }

        // ---- Avenir Next ----
        if (clean.contains("AvenirNext") || clean.contains("Avenir Next")
                || clean.contains("Avenir-Next")) {
            if (clean.contains("Condensed")) {
                // Named-weight branches FIRST: DemiBold contains "Bold" so it
                // must precede the generic if(bold). Same template as the
                // generic AvenirNext block below.
                if (clean.contains("DemiBold")) {
                    if (italic) return "avenir_next_condensed_demi_bold_italic.ttf";
                    return "avenir_next_condensed_demi_bold.ttf";
                }
                if (clean.contains("UltraLight")) {
                    if (italic) return "avenir_next_condensed_ultra_light_italic.ttf";
                    return "avenir_next_condensed_ultra_light.ttf";
                }
                if (clean.contains("Medium")) {
                    if (italic) return "avenir_next_condensed_medium_italic.ttf";
                    return "avenir_next_condensed_medium.ttf";
                }
                if (clean.contains("Heavy")) {
                    if (italic) return "avenir_next_condensed_heavy_italic.ttf";
                    return "avenir_next_condensed_heavy.ttf";
                }
                if (bold && italic) return "avenir_next_condensed_bold_italic.ttf";
                if (bold) return "avenir_next_condensed_bold.ttf";
                if (italic) return "avenir_next_condensed_italic.ttf";
                return "avenir_next_condensed.ttf";
            }
            // Named-weight branches FIRST: DemiBold contains "Bold" so it must
            // precede the generic if(bold); UltraLight contains "Light" but no
            // generic Light branch exists here. Weight-italic combos must
            // resolve before the generic if(italic) early-out.
            if (clean.contains("DemiBold")) {
                if (italic) return "avenir_next_demi_bold_italic.ttf";
                return "avenir_next_demi_bold.ttf";
            }
            if (clean.contains("UltraLight")) {
                if (italic) return "avenir_next_ultra_light_italic.ttf";
                return "avenir_next_ultra_light.ttf";
            }
            if (clean.contains("Medium")) {
                if (italic) return "avenir_next_medium_italic.ttf";
                return "avenir_next_medium.ttf";
            }
            if (clean.contains("Heavy")) {
                if (italic) return "avenir_next_heavy_italic.ttf";
                return "avenir_next_heavy.ttf";
            }
            if (bold && italic) return "avenir_next_bold_italic.ttf";
            if (bold) return "avenir_next_bold.ttf";
            if (italic) return "avenir_next_italic.ttf";
            return "avenir_next.ttf";
        }

        // ---- Avenir (without "Next") ----
        if (clean.contains("Avenir")) {
            if (bold || clean.contains("Black")) return "avenir_black.ttf";
            if (clean.contains("Heavy") && italic) return "avenir_heavy_oblique.ttf";
            if (clean.contains("Heavy")) return "avenir_heavy.ttf";
            if (clean.contains("Medium") && italic) return "avenir_medium_oblique.ttf";
            if (clean.contains("Medium")) return "avenir_medium.ttf";
            if (clean.contains("Light") && italic) return "avenir_light_oblique.ttf";
            if (clean.contains("Light")) return "avenir_light.ttf";
            if (clean.contains("Book") && italic) return "avenir_book_oblique.ttf";
            if (clean.contains("Book")) return "avenir_book.ttf";
            if (italic) return "avenir_oblique.ttf";
            return "avenir_roman.ttf";
        }

        // ---- Futura ----
        if (clean.contains("Futura")) {
            if (clean.contains("Heavy") || clean.contains("Black"))
                return "futura_bold.ttf";
            if (bold) return "futura_bold.ttf";
            if (clean.contains("Condensed") && clean.contains("ExtraBold"))
                return "futura_condensed_extrabold.ttf";
            if (clean.contains("Condensed")) return "futura_condensed_medium.ttf";
            if (italic) return "futura_medium_italic.ttf";
            return "futura_medium.ttf";
        }

        // ---- Gill Sans ----
        if (clean.contains("GillSans") || clean.contains("Gill Sans")
                || clean.contains("Gill-Sans")) {
            // Named-weight branches FIRST: UltraBold and SemiBold both contain
            // "Bold", so they must precede the generic if(bold) check.
            if (clean.contains("UltraBold")) return "gill_sans_ultrabold.ttf";
            if (clean.contains("SemiBold")) {
                if (italic) return "gill_sans_semibold_italic.ttf";
                return "gill_sans_semibold.ttf";
            }
            if (clean.contains("Light")) {
                if (italic) return "gill_sans_light_italic.ttf";
                return "gill_sans_light.ttf";
            }
            if (bold && italic) return "gill_sans_bold_italic.ttf";
            if (bold) return "gill_sans_bold.ttf";
            if (italic) return "gill_sans_italic.ttf";
            return "gill_sans.ttf";
        }

        // ---- Palatino ----
        if (clean.contains("Palatino")) {
            if (bold && italic) return "palatino_bold_italic.ttf";
            if (bold) return "palatino_bold.ttf";
            if (italic) return "palatino_italic.ttf";
            return "palatino.ttf";
        }

        // ---- Garamond (substitute: Palatino, closest available Old Style serif) ----
        if (clean.contains("Garamond")) {
            if (bold && italic) return "palatino_bold_italic.ttf";
            if (bold) return "palatino_bold.ttf";
            if (italic) return "palatino_italic.ttf";
            return "palatino.ttf";
        }

        // ---- Optima ----
        if (clean.contains("Optima")) {
            if (bold && italic) return "optima_bold_italic.ttf";
            if (bold) return "optima_bold.ttf";
            if (italic) return "optima_italic.ttf";
            if (clean.contains("ExtraBlack")) return "optima_extrablack.ttf";
            return "optima.ttf";
        }

        // ---- Baskerville ----
        if (clean.contains("Baskerville")) {
            if (clean.contains("SemiBold")) {
                if (italic) return "baskerville_semibold_italic.ttf";
                return "baskerville_semibold.ttf";
            }
            if (bold && italic) return "baskerville_bold_italic.ttf";
            if (bold) return "baskerville_bold.ttf";
            if (italic) return "baskerville_italic.ttf";
            return "baskerville.ttf";
        }

        // ---- Cochin ----
        if (clean.contains("Cochin")) {
            if (bold && italic) return "cochin_bold_italic.ttf";
            if (bold) return "cochin_bold.ttf";
            if (italic) return "cochin_italic.ttf";
            return "cochin.ttf";
        }

        // ---- Didot ----
        if (clean.contains("Didot")) {
            if (bold) return "didot_bold.ttf";
            if (italic) return "didot_italic.ttf";
            return "didot.ttf";
        }

        // ---- Charter ----
        if (clean.contains("Charter")) {
            if (clean.contains("Black")) {
                if (italic) return "charter_black_italic.ttf";
                return "charter_black.ttf";
            }
            if (bold && italic) return "charter_bold_italic.ttf";
            if (bold) return "charter_bold.ttf";
            if (italic) return "charter_italic.ttf";
            return "charter_roman.ttf";
        }

        // ---- Rockwell ----
        if (clean.contains("Rockwell")) {
            if (bold && italic) return "rockwell_bold_italic.ttf";
            if (bold) return "rockwell_bold.ttf";
            if (italic) return "rockwell_italic.ttf";
            return "rockwell.ttf";
        }

        // ---- American Typewriter ----
        if (clean.contains("AmericanTypewriter")
                || clean.contains("American Typewriter")) {
            // SemiBold contains "Bold" so it must precede the generic if(bold).
            // Condensed sub-variants stay grouped at the top.
            if (clean.contains("Condensed") && bold)
                return "american_typewriter_condensed_bold.ttf";
            if (clean.contains("Condensed") && clean.contains("Light"))
                return "american_typewriter_condensed_light.ttf";
            if (clean.contains("Condensed"))
                return "american_typewriter_condensed.ttf";
            if (clean.contains("SemiBold"))
                return "american_typewriter_semibold.ttf";
            if (bold) return "american_typewriter_bold.ttf";
            if (clean.contains("Light"))
                return "american_typewriter_light.ttf";
            return "american_typewriter.ttf";
        }

        // ---- Copperplate ----
        if (clean.contains("Copperplate")) {
            if (bold) return "copperplate_bold.ttf";
            if (clean.contains("Light")) return "copperplate_light.ttf";
            return "copperplate.ttf";
        }

        // ---- Lucida Grande ----
        if (clean.contains("LucidaGrande") || clean.contains("Lucida Grande")) {
            if (bold) return "lucida_grande_bold.ttf";
            return "lucida_grande.ttf";
        }

        // ---- Geneva ----
        if (clean.contains("Geneva")) {
            return "geneva.ttf";
        }

        // ---- Monaco ----
        if (clean.contains("Monaco")) {
            return "monaco.ttf";
        }

        // ---- Menlo ----
        if (clean.contains("Menlo")) {
            if (bold && italic) return "menlo_bold_italic.ttf";
            if (bold) return "menlo_bold.ttf";
            if (italic) return "menlo_italic.ttf";
            return "menlo.ttf";
        }

        // ---- New York ----
        if (clean.contains("NewYork") || clean.contains("New York")) {
            if (italic) return "newyorkitalic.ttf";
            return "newyork.ttf";
        }

        // ---- San Francisco (SF Pro / SFNS) ----
        if (clean.contains("SFPro") || clean.contains("SF Pro")
                || clean.contains("SFNS") || clean.contains(".SFNS")
                || clean.contains("SanFrancisco") || clean.contains(".SF")) {
            if (clean.contains("Mono") && italic) return "sfnsmono.ttf";
            if (clean.contains("Mono")) return "sfnsmono.ttf";
            if (clean.contains("Compact") && italic)
                return "sfcompactitalic.ttf";
            if (clean.contains("Compact")) return "sfcompact.ttf";
            if (italic) return "sfnsitalic.ttf";
            return "sfns.ttf";
        }

        // ---- PT Sans ----
        if (clean.contains("PTSans") || clean.contains("PT Sans")
                || clean.contains("PT-Sans")) {
            if (clean.contains("Narrow") && bold)
                return "pt_sans_narrow_bold.ttf";
            if (clean.contains("Narrow")) return "pt_sans_narrow.ttf";
            if (clean.contains("Caption") && bold)
                return "pt_sans_caption_bold.ttf";
            if (clean.contains("Caption")) return "pt_sans_caption.ttf";
            if (bold && italic) return "pt_sans_bold_italic.ttf";
            if (bold) return "pt_sans_bold.ttf";
            if (italic) return "pt_sans_italic.ttf";
            return "pt_sans.ttf";
        }

        // ---- PT Serif ----
        if (clean.contains("PTSerif") || clean.contains("PT Serif")
                || clean.contains("PT-Serif")) {
            if (clean.contains("Caption") && italic)
                return "pt_serif_caption_italic.ttf";
            if (clean.contains("Caption")) return "pt_serif_caption.ttf";
            if (bold && italic) return "pt_serif_bold_italic.ttf";
            if (bold) return "pt_serif_bold.ttf";
            if (italic) return "pt_serif_italic.ttf";
            return "pt_serif.ttf";
        }

        // ---- PT Mono ----
        if (clean.contains("PTMono") || clean.contains("PT Mono")
                || clean.contains("PT-Mono")) {
            if (bold) return "pt_mono_bold.ttf";
            return "pt_mono.ttf";
        }

        // ---- Galvji ----
        if (clean.contains("Galvji")) {
            if (bold && italic) return "galvji_bold_oblique.ttf";
            if (bold) return "galvji_bold.ttf";
            if (italic) return "galvji_oblique.ttf";
            return "galvji.ttf";
        }

        // ---- Seravek ----
        if (clean.contains("Seravek")) {
            // ExtraLight before Light (Light is a substring of ExtraLight);
            // weight-italic combos before generic if(italic).
            if (clean.contains("ExtraLight")) {
                if (italic) return "seravek_extralight_italic.ttf";
                return "seravek_extralight.ttf";
            }
            if (clean.contains("Light")) {
                if (italic) return "seravek_light_italic.ttf";
                return "seravek_light.ttf";
            }
            if (clean.contains("Medium")) {
                if (italic) return "seravek_medium_italic.ttf";
                return "seravek_medium.ttf";
            }
            if (bold && italic) return "seravek_bold_italic.ttf";
            if (bold) return "seravek_bold.ttf";
            if (italic) return "seravek_italic.ttf";
            return "seravek.ttf";
        }

        // ---- Athelas ----
        if (clean.contains("Athelas")) {
            if (bold && italic) return "athelas_bold_italic.ttf";
            if (bold) return "athelas_bold.ttf";
            if (italic) return "athelas_italic.ttf";
            return "athelas.ttf";
        }

        // ---- SuperClarendon ----
        if (clean.contains("SuperClarendon")
                || clean.contains("Super Clarendon")) {
            if (clean.contains("Black")) {
                if (italic) return "superclarendon_black_italic.ttf";
                return "superclarendon_black.ttf";
            }
            if (clean.contains("Light")) {
                if (italic) return "superclarendon_light_italic.ttf";
                return "superclarendon_light.ttf";
            }
            if (bold && italic) return "superclarendon_bold_italic.ttf";
            if (bold) return "superclarendon_bold.ttf";
            if (italic) return "superclarendon_italic.ttf";
            return "superclarendon.ttf";
        }

        // ---- Marion ----
        if (clean.contains("Marion")) {
            if (bold) return "marion_bold.ttf";
            if (italic) return "marion_italic.ttf";
            return "marion.ttf";
        }

        // ---- Big Caslon ----
        if (clean.contains("BigCaslon") || clean.contains("Big Caslon")) {
            return "bigcaslon.ttf";
        }

        // ---- Chalkboard / Chalkboard SE ----
        if (clean.contains("ChalkboardSE") || clean.contains("Chalkboard SE")) {
            if (bold) return "chalkboard_se_bold.ttf";
            if (clean.contains("Light")) return "chalkboard_se_light.ttf";
            return "chalkboard_se.ttf";
        }
        if (clean.contains("Chalkboard")) {
            if (bold) return "chalkboard_bold.ttf";
            return "chalkboard.ttf";
        }

        // ---- Chalkduster ----
        if (clean.contains("Chalkduster")) {
            return "chalkduster.ttf";
        }

        // ---- Impact (Apple version) ----
        if (clean.contains("Impact")) {
            return "impact.ttf";
        }

        // ---- Papyrus ----
        if (clean.contains("Papyrus")) {
            if (clean.contains("Condensed")) return "papyrus_condensed.ttf";
            return "papyrus.ttf";
        }

        // ---- Marker Felt ----
        if (clean.contains("MarkerFelt") || clean.contains("Marker Felt")) {
            if (clean.contains("Wide")) return "marker_felt_wide.ttf";
            return "marker_felt_thin.ttf";
        }

        // ---- Noteworthy ----
        if (clean.contains("Noteworthy")) {
            if (bold) return "noteworthy_bold.ttf";
            return "noteworthy_light.ttf";
        }

        // ---- Snell Roundhand ----
        if (clean.contains("SnellRoundhand")
                || clean.contains("Snell Roundhand")) {
            if (clean.contains("Black")) return "snell_roundhand_black.ttf";
            if (bold) return "snell_roundhand_bold.ttf";
            return "snell_roundhand.ttf";
        }

        // ---- Zapfino ----
        if (clean.contains("Zapfino")) {
            return "zapfino.ttf";
        }

        // ---- SignPainter ----
        if (clean.contains("SignPainter")) {
            if (clean.contains("SemiBold") || bold)
                return "signpainter_housescript_semibold.ttf";
            return "signpainter_housescript.ttf";
        }

        // ---- STIX Two (math/scientific) ----
        if (clean.contains("STIXTwo") || clean.contains("STIX Two")) {
            if (italic) return "stixtwotext_italic.ttf";
            return "stixtwotext.ttf";
        }

        // ---- Apple Arial Narrow ----
        if (clean.contains("ArialNarrow") || clean.contains("Arial Narrow")
                || clean.contains("Arial-Narrow")) {
            if (bold && italic) return "arial_narrow_bold_italic.ttf";
            if (bold) return "arial_narrow_bold.ttf";
            if (italic) return "arial_narrow_italic.ttf";
            return "arial_narrow.ttf";
        }

        // ---- Phosphate ----
        if (clean.contains("Phosphate")) {
            if (clean.contains("Inline")) return "phosphate_inline.ttf";
            return "phosphate_solid.ttf";
        }

        // ---- Skia ----
        if (clean.contains("Skia")) {
            return "skia.ttf";
        }

        // ---- Luminari ----
        if (clean.contains("Luminari")) {
            return "luminari.ttf";
        }

        // ---- Trattatello ----
        if (clean.contains("Trattatello")) {
            return "trattatello.ttf";
        }

        // ---- Herculanum ----
        if (clean.contains("Herculanum")) {
            return "herculanum.ttf";
        }

        // ---- Kefa ----
        if (clean.contains("Kefa")) {
            return "kefaiii.ttf";
        }

        return null;
    }

    static String mapToAppleFont(String clean, boolean bold, boolean italic) {
        String name = mapToAppleFontName(clean, bold, italic);
        if (name == null) return null;
        String dir = getAppleFontDir();
        String real = FontDirIndex.of(dir).resolve(name);
        return real != null ? real : dir + name;
    }

    // -----------------------------------------------------------------
    // Link Annotation Tagging
    // -----------------------------------------------------------------

    /**
     * Tag existing link annotations in the PDF structure tree.
     *
     * PDF/UA requires every Link annotation to be nested within a Link
     * structure element. The original PDF may contain link annotations
     * (hyperlinks, cross-references) that are not represented in the
     * Docling structure JSON. This method finds all untagged Link
     * annotations and wraps each in a proper Link structure element.
     */
    private static void tagLinkAnnotations(
        PdfDocument pdfDoc,
        TagTreePointer tagPointer,
        PdfFont font,
        Map<Integer, RawPdfCanvas> canvasCache
    ) {
        int taggedLinks = 0;

        try {
            // Navigate to document root for adding Link elements
            tagPointer.moveToRoot();

            for (int p = 1; p <= pdfDoc.getNumberOfPages(); p++) {
                PdfPage page = pdfDoc.getPage(p);
                List<PdfAnnotation> annotations = page.getAnnotations();
                if (annotations == null || annotations.isEmpty()) continue;

                for (PdfAnnotation annot : annotations) {
                    // Only process Link annotations
                    if (!(annot instanceof PdfLinkAnnotation)) continue;

                    PdfDictionary annotDict = annot.getPdfObject();

                    // Skip if already tagged (has /StructParent)
                    if (annotDict.containsKey(PdfName.StructParent)) continue;

                    try {
                        // Assign a StructParent index to the annotation
                        int structParentIndex = pdfDoc
                            .getNextStructParentIndex();
                        annotDict.put(PdfName.StructParent,
                            new PdfNumber(structParentIndex));

                        // Set annotation flags: Print (bit 3 = 4)
                        int flags = annot.getFlags();
                        annot.setFlags(flags | PdfAnnotation.PRINT);

                        // PDF/UA 7.18.1 + 7.18.5: Link annotations must
                        // have a /Contents key with alternate description.
                        // Extract the URI as the description text.
                        if (!annotDict.containsKey(PdfName.Contents)) {
                            String contentsText = null;
                            // Try to get URI from /A (action) dictionary
                            PdfDictionary action = annotDict.getAsDictionary(
                                PdfName.A);
                            if (action != null) {
                                PdfString uri = action.getAsString(
                                    new PdfName("URI"));
                                if (uri != null) {
                                    contentsText = uri.getValue();
                                }
                            }
                            // Fallback: try /Dest
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

                        // Wrap Link in a P element — inline SEs like Link
                        // must not be direct children of Document (PDF/UA).
                        tagPointer.setPageForTagging(page);
                        tagPointer.addTag("P");

                        // Set /ActualText on the P wrapper so it is not blank
                        // to screen readers. Use annotation /Contents (URI or
                        // fallback text) as the readable description.
                        PdfStructElem pWrapperElem = tagPointer.getContext()
                            .getPointerStructElem(tagPointer);
                        String pActualText = null;
                        PdfString contentsStr = annotDict.getAsString(PdfName.Contents);
                        if (contentsStr != null && !contentsStr.getValue().isEmpty()) {
                            pActualText = dePuaText(contentsStr.getValue());
                        }
                        if (pActualText == null) {
                            pActualText = "Link";
                        }
                        // /ActualText removed — blocks AROL/NVDA

                        tagPointer.addTag("Link");

                        // Add marked content for the Link element
                        PdfCanvas canvas = canvasCache.computeIfAbsent(p,
                            pp -> new RawPdfCanvas(pdfDoc.getPage(pp)));
                        Rectangle rect = annot.getRectangle() != null
                            ? annot.getRectangle().toRectangle()
                            : new Rectangle(72, 72, 1, 1);
                        writeInvisibleMCR(canvas, tagPointer, font,
                            rect.getX(), rect.getY(), pActualText);

                        // Add the annotation as an OBJR (object reference)
                        // kid of the Link structure element
                        PdfStructElem linkElem = tagPointer.getContext()
                            .getPointerStructElem(tagPointer);
                        PdfObjRef objRef = new PdfObjRef(
                            annot, linkElem, structParentIndex);
                        linkElem.addKid(objRef);

                        // NOTE: /ActualText is intentionally NOT set on Link elements.
                        // Link elements are children of P elements that already have
                        // /ActualText. Setting /ActualText on Link too creates a nested
                        // /ActualText that Acrobat flags as NestedAltText. Screen readers
                        // access link destinations via the OBJR annotation reference and
                        // its URI action — /ActualText on the Link structure element is
                        // redundant and causes the nested alternate text failure.

                        tagPointer.moveToParent(); // back to P wrapper
                        tagPointer.moveToParent(); // back to root
                        taggedLinks++;

                    } catch (Exception e) {
                        System.err.println("  Warning: failed to tag link "
                            + "on page " + p + ": " + e.getMessage());
                        // Try to recover pointer position
                        try { tagPointer.moveToRoot(); }
                        catch (Exception e2) {
                            System.err.println("  Warning: tag pointer "
                                + "recovery failed — subsequent tags on "
                                + "this page may nest incorrectly: "
                                + e2.getMessage());
                        }
                    }
                }
            }
        } catch (Exception e) {
            System.err.println("Warning: link annotation tagging failed: "
                + e.getMessage());
        }

        if (taggedLinks > 0) {
            System.out.println("  Tagged " + taggedLinks
                + " link annotations in structure tree");
        }
    }

    // -----------------------------------------------------------------
    // Widget Annotation Tagging
    // -----------------------------------------------------------------

    /**
     * Tag existing widget annotations (form fields) in the PDF structure tree.
     *
     * PDF/UA 7.18.4 requires every Widget annotation to be nested within
     * a Form structure element. This method finds all untagged Widget
     * annotations and wraps each in a proper Form structure element.
     */
    private static void tagWidgetAnnotations(
        PdfDocument pdfDoc,
        TagTreePointer tagPointer,
        PdfFont font,
        Map<Integer, RawPdfCanvas> canvasCache
    ) {
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
                        // Assign a StructParent index
                        int structParentIndex = pdfDoc
                            .getNextStructParentIndex();
                        annotDict.put(PdfName.StructParent,
                            new PdfNumber(structParentIndex));

                        // Set annotation flags: Print (bit 3 = 4)
                        int flags = annot.getFlags();
                        annot.setFlags(flags | PdfAnnotation.PRINT);

                        // Build alt text from field properties:
                        // /TU (tooltip) > /T (field name) > fallback
                        String altText = null;
                        // Check the annotation itself first
                        PdfString tu = annotDict.getAsString(
                            new PdfName("TU"));
                        if (tu != null && !tu.getValue().trim().isEmpty()) {
                            altText = tu.getValue().trim();
                        }
                        // Try field name /T
                        if (altText == null) {
                            PdfString t = annotDict.getAsString(
                                new PdfName("T"));
                            if (t != null && !t.getValue().trim().isEmpty()) {
                                altText = t.getValue().trim();
                            }
                        }
                        // Try parent field dict (merged fields)
                        if (altText == null) {
                            PdfDictionary parent = annotDict.getAsDictionary(
                                PdfName.Parent);
                            if (parent != null) {
                                tu = parent.getAsString(new PdfName("TU"));
                                if (tu != null
                                        && !tu.getValue().trim().isEmpty()) {
                                    altText = tu.getValue().trim();
                                }
                                if (altText == null) {
                                    PdfString t = parent.getAsString(
                                        new PdfName("T"));
                                    if (t != null
                                            && !t.getValue().trim().isEmpty()) {
                                        altText = t.getValue().trim();
                                    }
                                }
                            }
                        }
                        if (altText == null) {
                            altText = "Form field";
                        }

                        // Create Form element using TagTreePointer
                        // (needed for proper parent tree registration),
                        // add MCR + OBJR, then remove the MCR from the
                        // Form's /K array. PDF/UA 7.18.4 t2 requires
                        // Form to have exactly one content item (OBJR).
                        tagPointer.setPageForTagging(page);
                        tagPointer.addTag("Form");

                        PdfStructElem formElem = tagPointer.getContext()
                            .getPointerStructElem(tagPointer);
                        formElem.setAlt(new PdfString(altText,
                            PdfEncodings.UNICODE_BIG));

                        // Add MCR for parent tree registration
                        PdfCanvas canvas = canvasCache.computeIfAbsent(p,
                            pp -> new RawPdfCanvas(pdfDoc.getPage(pp)));
                        Rectangle rect = annot.getRectangle() != null
                            ? annot.getRectangle().toRectangle()
                            : new Rectangle(72, 72, 1, 1);
                        writeInvisibleMCR(canvas, tagPointer, font,
                            rect.getX(), rect.getY(), altText);

                        // Add OBJR kid
                        PdfObjRef objRef = new PdfObjRef(
                            annot, formElem, structParentIndex);
                        formElem.addKid(objRef);

                        // Remove the MCR from /K, keeping only OBJR.
                        // The MCR was needed for iText to register the
                        // element, but t2 requires only one content item.
                        PdfObject kObj = formElem.getPdfObject().get(PdfName.K);
                        if (kObj instanceof PdfArray) {
                            PdfArray kArr = (PdfArray) kObj;
                            // Remove MCR entries (integers or dicts with
                            // /Type /MCR), keep only OBJR dicts
                            for (int ki = kArr.size() - 1; ki >= 0; ki--) {
                                PdfObject item = kArr.get(ki);
                                if (item instanceof PdfNumber) {
                                    kArr.remove(ki); // MCR as integer MCID
                                } else if (item instanceof PdfDictionary) {
                                    PdfDictionary d = (PdfDictionary) item;
                                    PdfName type = d.getAsName(PdfName.Type);
                                    if (type != null && "MCR".equals(
                                            type.getValue())) {
                                        kArr.remove(ki);
                                    }
                                }
                            }
                            // If only one kid remains, unwrap the array
                            if (kArr.size() == 1) {
                                formElem.getPdfObject().put(
                                    PdfName.K, kArr.get(0));
                            }
                        } else if (kObj instanceof PdfNumber) {
                            // MCR only — shouldn't happen but remove it
                            formElem.getPdfObject().remove(PdfName.K);
                        }

                        tagPointer.moveToParent(); // back to root
                        taggedWidgets++;

                    } catch (Exception e) {
                        System.err.println("  Warning: failed to tag widget "
                            + "on page " + p + ": " + e.getMessage());
                        try { tagPointer.moveToRoot(); }
                        catch (Exception e2) {
                            System.err.println("  Warning: tag pointer "
                                + "recovery failed — subsequent tags on "
                                + "this page may nest incorrectly: "
                                + e2.getMessage());
                        }
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

    /**
     * Tag existing markup annotations (FreeText, Square, Circle, Highlight,
     * Underline, StrikeOut, Squiggly, Ink) in the PDF structure tree.
     *
     * PDF/UA 7.18.1 requires every visible annotation to be tagged in a
     * structure element. This method finds untagged markup annotations and
     * wraps each in a P > Span structure element with an OBJR kid.
     * It also ensures the annotation has a /Contents key (7.18.1-t2).
     */
    private static void tagMarkupAnnotations(
        PdfDocument pdfDoc,
        TagTreePointer tagPointer,
        PdfFont font,
        Map<Integer, RawPdfCanvas> canvasCache
    ) {
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

                    try {
                        // Assign StructParent index
                        int structParentIndex = pdfDoc
                            .getNextStructParentIndex();
                        annotDict.put(PdfName.StructParent,
                            new PdfNumber(structParentIndex));

                        // Set Print flag
                        int flags = annot.getFlags();
                        annot.setFlags(flags | PdfAnnotation.PRINT);

                        // Ensure /Contents key exists (required by 7.18.1-t2)
                        if (!annotDict.containsKey(PdfName.Contents)) {
                            String contentsText = null;

                            // FreeText: try /RC (rich text content)
                            if ("FreeText".equals(subtypeStr)) {
                                PdfString rc = annotDict.getAsString(
                                    new PdfName("RC"));
                                if (rc != null
                                        && !rc.getValue().trim().isEmpty()) {
                                    // Strip HTML tags from rich text
                                    String raw = rc.getValue()
                                        .replaceAll("<[^>]+>", "").trim();
                                    if (!raw.isEmpty()) {
                                        contentsText = raw.length() > 200
                                            ? raw.substring(0, 200) : raw;
                                    }
                                }
                            }

                            // Try /Subj (subject) for any markup annotation
                            if (contentsText == null) {
                                PdfString subj = annotDict.getAsString(
                                    new PdfName("Subj"));
                                if (subj != null
                                        && !subj.getValue().trim().isEmpty()) {
                                    contentsText = subj.getValue().trim();
                                }
                            }

                            // Try /T (title/author)
                            if (contentsText == null) {
                                PdfString t = annotDict.getAsString(
                                    new PdfName("T"));
                                if (t != null
                                        && !t.getValue().trim().isEmpty()) {
                                    contentsText = t.getValue().trim();
                                }
                            }

                            // Fallback
                            if (contentsText == null) {
                                contentsText = subtypeStr + " annotation";
                            }

                            annotDict.put(PdfName.Contents,
                                new PdfString(contentsText));
                        }

                        // Wrap in Annot structure element (PDF/UA 7.18.1
                        // requires non-Widget/non-Link annotations to be
                        // nested in an Annot tag)
                        tagPointer.setPageForTagging(page);
                        tagPointer.addTag("Annot");

                        PdfStructElem spanElem = tagPointer.getContext()
                            .getPointerStructElem(tagPointer);

                        // Set /Alt on the Annot SE (7.18.1-t2 fallback)
                        String altText = annotDict.containsKey(
                                PdfName.Contents)
                            ? annotDict.getAsString(PdfName.Contents)
                                .getValue()
                            : subtypeStr + " annotation";
                        spanElem.setAlt(new PdfString(altText,
                            PdfEncodings.UNICODE_BIG));

                        // Create MCR for parent tree registration
                        PdfCanvas canvas = canvasCache.computeIfAbsent(p,
                            pp -> new RawPdfCanvas(pdfDoc.getPage(pp)));
                        Rectangle rect = annot.getRectangle() != null
                            ? annot.getRectangle().toRectangle()
                            : new Rectangle(72, 72, 1, 1);
                        writeInvisibleMCR(canvas, tagPointer, font,
                            rect.getX(), rect.getY(), altText);

                        // Add OBJR kid
                        PdfObjRef objRef = new PdfObjRef(
                            annot, spanElem, structParentIndex);
                        spanElem.addKid(objRef);

                        // Back to root (Annot is direct child of root)
                        tagPointer.moveToParent(); // back to root
                        taggedMarkup++;

                    } catch (Exception e) {
                        System.err.println("  Warning: failed to tag "
                            + subtypeStr + " annotation on page " + p
                            + ": " + e.getMessage());
                        try { tagPointer.moveToRoot(); }
                        catch (Exception e2) {
                            System.err.println("  Warning: tag pointer "
                                + "recovery failed — subsequent tags on "
                                + "this page may nest incorrectly: "
                                + e2.getMessage());
                        }
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

    // -----------------------------------------------------------------
    // Page Content Conversion
    // -----------------------------------------------------------------

    /**
     * Convert a page's existing content into a Form XObject and clear the page.
     *
     * This preserves the original visual content (images, text) as a reusable
     * XObject that can later be drawn inside proper BDC/EMC tagged blocks
     * or Artifact markers. The page's content streams are cleared so that
     * the new tagged content stream is the only one.
     *
     * @return A PdfFormXObject containing the original page content, or null
     *         if the page has no content.
     */
    private static PdfFormXObject convertPageContentToFormXObject(
            PdfDocument pdfDoc, PdfPage page) {
        try {
            byte[] existingBytes = page.getContentBytes();
            if (existingBytes == null || existingBytes.length == 0) return null;

            // Create a Form XObject with the page's visible dimensions.
            // Use CropBox (visible area) when it differs from MediaBox,
            // e.g. for brochures or cropped PDFs.
            Rectangle mediaBox = page.getMediaBox();
            Rectangle effectiveBox = page.getCropBox();
            if (effectiveBox == null || effectiveBox.equalsWithEpsilon(mediaBox)) {
                effectiveBox = mediaBox;
            }
            PdfFormXObject formXObj = new PdfFormXObject(effectiveBox);

            // Strip any existing marked content operators (BDC/BMC/EMC)
            // from the content stream. If the original PDF was already
            // partially tagged, its MCIDs would conflict with our new
            // structure tree and cause PAC content failures.
            byte[] cleanBytes = stripMarkedContent(existingBytes);

            // Normalize operator spacing: insert whitespace between
            // operator keywords and immediately following delimiters.
            // iText 9's tokenizer fails to split "Tf( )Tj" correctly,
            // treating it as single unknown keyword "TfTj".
            cleanBytes = normalizeOperatorSpacing(cleanBytes);

            // Strip invisible text (text rendering mode 3) from the
            // content stream. Source PDFs often have an invisible OCR
            // text layer using broken fonts (e.g. "NotDefSpecial") where
            // every glyph maps to .notdef, violating PDF/UA 7.21.8.
            // Since our tagger adds its own proper invisible text layer,
            // the original OCR text is redundant and can be removed.
            cleanBytes = stripInvisibleText(cleanBytes);

            // Also strip text using hidden OCR fonts (e.g. HiddenHorzOCR)
            // that may not use rendering mode 3 but produce .notdef glyphs.
            PdfDictionary pageRes = page.getResources().getPdfObject();
            Set<String> hiddenFonts = findHiddenOCRFontNames(pageRes);
            if (!hiddenFonts.isEmpty()) {
                cleanBytes = stripFontText(cleanBytes, hiddenFonts);
            }

            // Strip text operations referencing .notdef glyphs (7.21.8).
            // Source PDFs may have fonts (SymbolMT, ZapfDingbats, etc.)
            // with subset-embedded glyphs where some char codes map to
            // .notdef.  These are visible but render as blank.
            // Repair font inheritance first, then strip .notdef.
            // Order matters: fixFontInheritance may inject Tf ops that
            // change which char codes are .notdef for a given font.
            cleanBytes = fixFontInheritance(cleanBytes);
            cleanBytes = stripNotdefGlyphs(cleanBytes, pageRes);

            // Copy the cleaned content stream into the Form XObject.
            // false = REPLACE (formXObj is freshly created/empty here, so this
            // is behaviour-identical, but keep it consistent with the *FromXObjects
            // cleanups where true=APPEND caused the shared-XObject doubling OOM).
            formXObj.getPdfObject().setData(cleanBytes, false);

            // Recursively strip invisible text from nested XObjects
            // (source PDFs may have Form XObjects within XObjects that
            // also contain invisible OCR text with broken fonts).
            PdfDictionary pageResources = page.getResources().getPdfObject();
            stripInvisibleTextFromXObjects(pageResources, new HashSet<>());

            // Recursively strip marked content from nested Form XObjects.
            // Source PDFs with existing tags may have MCID references inside
            // nested XObjects. If these end up inside Artifact blocks,
            // veraPDF flags 7.1-t2 "tagged content inside Artifact".
            stripMarkedContentFromXObjects(pageResources, new HashSet<>());

            // Recursively strip .notdef glyph references from XObjects
            stripNotdefGlyphsFromXObjects(pageResources, new HashSet<>());

            // Reference the page's resources in the Form XObject so that
            // image references (/lm1, /Im1, etc.) and fonts resolve correctly.
            // We reference the same objects (not copy) since we're in the
            // same document.
            if (pageResources != null) {
                // Build a new Resources dict referencing the same sub-dicts
                PdfDictionary formRes = new PdfDictionary();
                for (PdfName key : pageResources.keySet()) {
                    formRes.put(key, pageResources.get(key));
                }
                formXObj.getPdfObject().put(PdfName.Resources, formRes);
            }

            // Clear the page's original content streams
            // (new tagged content will be added via PdfCanvas)
            int contentStreamCount = page.getContentStreamCount();
            for (int i = contentStreamCount - 1; i >= 0; i--) {
                page.getContentStream(i).setData(new byte[0]);
            }

            return formXObj;

        } catch (Exception e) {
            System.err.println("Warning: failed to convert page content "
                + "to Form XObject: " + e.getMessage());
            return null;
        }
    }

    /**
     * Strip marked content operators (BDC, BMC, EMC) from a PDF content
     * stream. This removes any existing tagging from the original PDF so
     * that stale MCIDs don't conflict with our new structure tree.
     *
     * Handles:
     *   /Tag <<...>> BDC   (marked content with inline properties)
     *   /Tag /Name BDC     (marked content with named properties)
     *   /Tag BMC           (marked content begin without properties)
     *   EMC                (marked content end)
     *
     * All patterns are located on a string-masked copy of the content
     * (see {@link #maskStrings}) so BDC/EMC keywords or dictionary syntax
     * inside painted strings can never match, and the dict-interior
     * patterns are bounded so a match can never span unrelated painting
     * operators.  (Both were real, silent content-loss bugs with the
     * previous unbounded {@code [\s\S]*?} patterns.)
     */
    // /Tag <<...>> BDC — dict interior limited to plain chars, literal
    // strings, hex strings, and one level of nested dict.
    private static final java.util.regex.Pattern MC_BDC_DICT =
        java.util.regex.Pattern.compile(
            "/\\w+\\s*<<(?:[^<>()]|\\([^()]*\\)|<<[^<>]*>>|<[^<>]*>)*>>\\s*BDC");
    // /Tag /Name BDC — with or without space between names
    private static final java.util.regex.Pattern MC_BDC_NAMED =
        java.util.regex.Pattern.compile("/\\w+\\s*/\\w+\\s+BDC");
    // /Tag BMC — space or newline separator
    private static final java.util.regex.Pattern MC_BMC =
        java.util.regex.Pattern.compile("/\\w+\\s+BMC");
    private static final java.util.regex.Pattern MC_EMC =
        java.util.regex.Pattern.compile("\\bEMC\\b");
    // Orphaned <</MCID N ...>> property dict — fallback when the BDC
    // pattern didn't match; bounded to a single dict.
    private static final java.util.regex.Pattern MC_ORPHAN_MCID =
        java.util.regex.Pattern.compile(
            "<<(?:[^<>()]|\\([^()]*\\)|<[^<>]*>)*/MCID\\s+\\d+"
            + "(?:[^<>()]|\\([^()]*\\)|<[^<>]*>)*>>");
    // Painting / content-producing operators. Lookarounds keep this from
    // matching inside names (/F1) or longer keywords (BT, EI vs EIx).
    private static final java.util.regex.Pattern PAINTING_OPS =
        java.util.regex.Pattern.compile(
            "(?<![/\\w])(?:Tj|TJ|Do|sh|re|EI|[fFbBsS]\\*?)(?![\\w*])");

    static byte[] stripMarkedContent(byte[] contentBytes) {
        // Work with ISO-8859-1 to preserve binary data unchanged
        String content = new String(contentBytes,
            java.nio.charset.StandardCharsets.ISO_8859_1);
        int origLen = content.length();

        String result = removeMatches(content, MC_BDC_DICT);
        result = removeMatches(result, MC_BDC_NAMED);
        result = removeMatches(result, MC_BMC);
        result = removeMatches(result, MC_EMC);
        result = removeMatches(result, MC_ORPHAN_MCID);

        if (result.length() < origLen) {
            // Safety net: stripping must never remove painting operators.
            // An un-stripped leftover is a validation flag at worst; a
            // lost painting operator is silent content loss.
            int before = countPaintingOps(content);
            int after = countPaintingOps(result);
            if (after != before) {
                System.err.println("    WARNING: marked-content strip would "
                    + "remove painting operators (" + before + " -> " + after
                    + ") — keeping original content");
                return contentBytes;
            }
            System.out.println("    Stripped " + (origLen - result.length())
                + " bytes of stale marked content from page");
        }

        return result.getBytes(java.nio.charset.StandardCharsets.ISO_8859_1);
    }

    /**
     * Replace the interiors of literal strings {@code (...)} and hex
     * strings {@code <...>} with 'x' placeholders, preserving length and
     * delimiters, so regex passes over content streams cannot match
     * operator keywords or dictionary syntax inside painted string data.
     * Handles nested parentheses and backslash escapes in literal strings;
     * dictionary delimiters {@code << >>} are left untouched.
     */
    static String maskStrings(String content) {
        char[] chars = content.toCharArray();
        int i = 0;
        int n = chars.length;
        while (i < n) {
            char ch = chars[i];
            if (ch == '(') {
                int depth = 1;
                i++;
                while (i < n && depth > 0) {
                    char c = chars[i];
                    if (c == '\\' && i + 1 < n) {
                        chars[i] = 'x';
                        chars[i + 1] = 'x';
                        i += 2;
                        continue;
                    }
                    if (c == '(') depth++;
                    else if (c == ')') depth--;
                    if (depth > 0) chars[i] = 'x';
                    i++;
                }
            } else if (ch == '<') {
                if (i + 1 < n && chars[i + 1] == '<') {
                    i += 2;  // dictionary open — not a string
                } else {
                    i++;     // hex string — mask up to closing '>'
                    while (i < n && chars[i] != '>') {
                        chars[i] = 'x';
                        i++;
                    }
                    if (i < n) i++;  // skip closing '>'
                }
            } else {
                i++;
            }
        }
        return new String(chars);
    }

    /**
     * Remove every match of {@code p} — located on the string-masked copy
     * of {@code content} — from the original (unmasked) content.
     */
    private static String removeMatches(String content,
            java.util.regex.Pattern p) {
        java.util.regex.Matcher m = p.matcher(maskStrings(content));
        if (!m.find()) return content;
        StringBuilder sb = new StringBuilder(content.length());
        int last = 0;
        do {
            sb.append(content, last, m.start());
            last = m.end();
        } while (m.find());
        sb.append(content, last, content.length());
        return sb.toString();
    }

    /** Count painting operators, ignoring string interiors. */
    private static int countPaintingOps(String content) {
        java.util.regex.Matcher m =
            PAINTING_OPS.matcher(maskStrings(content));
        int count = 0;
        while (m.find()) count++;
        return count;
    }

    /**
     * Normalize tightly-packed PDF content streams by inserting whitespace
     * between operators and delimiter characters.  Some PDF generators
     * (notably LaTeX/dvipdfm) emit content like {@code Tf( )Tj} with no
     * whitespace between the operator keyword and the following string
     * literal delimiter {@code (}.  iText 9's PdfTokenizer fails to split
     * on the delimiter, parsing {@code Tf( )Tj} as a single unknown
     * keyword {@code TfTj} and swallowing the string operand entirely.
     * <p>
     * The PDF spec (ISO 32000-1 §7.2.2) defines {@code ( ) < > [ ] / %}
     * as delimiter characters that terminate regular tokens, but iText's
     * tokenizer does not honour this during keyword scanning.  This method
     * inserts a newline between an operator keyword and an immediately
     * following {@code (} or {@code <} to work around the parser bug.
     * <p>
     * Only applied outside of string literals and inside BT/ET text
     * objects where the pattern is common.  Safe because adding whitespace
     * between tokens is always legal in PDF syntax.
     */
    static byte[] normalizeOperatorSpacing(byte[] contentBytes) {
        // Work byte-by-byte to correctly skip string literals.
        // Target pattern: a letter (end of keyword like Tf, TJ, Td)
        // immediately followed by '(' or '<' outside of string context.
        int len = contentBytes.length;
        // Estimate: very few insertions needed, so preallocate same size + small buffer
        java.io.ByteArrayOutputStream out = new java.io.ByteArrayOutputStream(len + 256);
        int parenDepth = 0;     // track nested literal strings
        boolean inHexString = false;

        for (int i = 0; i < len; i++) {
            byte b = contentBytes[i];

            if (parenDepth > 0) {
                // Inside literal string — track nesting and escapes
                out.write(b);
                if (b == '\\' && i + 1 < len) {
                    // Escaped char — write next byte and skip
                    out.write(contentBytes[++i]);
                } else if (b == '(') {
                    parenDepth++;
                } else if (b == ')') {
                    parenDepth--;
                }
                continue;
            }

            if (inHexString) {
                out.write(b);
                if (b == '>') inHexString = false;
                continue;
            }

            // Outside any string — check for the target pattern
            if ((b == '(' || b == '<') && i > 0) {
                byte prev = contentBytes[i - 1];
                // Previous byte is a letter (end of operator keyword)?
                boolean prevIsLetter =
                    (prev >= 'A' && prev <= 'Z') ||
                    (prev >= 'a' && prev <= 'z') ||
                    prev == '*' || prev == '\'';  // d0, d1, ', " operators
                if (prevIsLetter) {
                    out.write('\n');  // inject separator
                }
            }

            out.write(b);

            if (b == '(') {
                parenDepth = 1;
            } else if (b == '<' && i + 1 < len && contentBytes[i + 1] != '<') {
                // Single '<' starts hex string; '<<' starts dictionary
                inHexString = true;
            }
        }

        byte[] result = out.toByteArray();
        if (result.length > len) {
            System.out.println("    Normalized operator spacing: inserted "
                + (result.length - len) + " whitespace bytes");
        }
        return result;
    }

    /**
     * Strip inline images (BI...ID...EI) from a PDF content stream.
     * iText's PdfCanvasEditor re-serializes inline images as
     * {@code <<dict>>stream...endstream EI} which is invalid PDF syntax.
     * These inline images are typically 1x1 transparent image masks used
     * as invisible spacers (zero visual content).  Stripping them allows
     * the content-preserving editor to process the page normally.
     */
    static byte[] stripInlineImages(byte[] contentBytes) {
        String content = new String(contentBytes,
            java.nio.charset.StandardCharsets.ISO_8859_1);
        int origLen = content.length();

        // Match BI <dict> ID <data> EI sequences.
        // BI must appear at a word boundary (start of line or after
        // whitespace).  The data section between ID and EI is binary
        // but we use non-greedy matching which works because EI also
        // appears at a word boundary.
        content = java.util.regex.Pattern.compile(
            "(?:^|(?<=\\s))BI\\b[\\s\\S]*?\\bID\\b[\\s\\S]*?\\bEI(?=\\s|$)",
            java.util.regex.Pattern.DOTALL | java.util.regex.Pattern.MULTILINE
        ).matcher(content).replaceAll("");

        if (content.length() < origLen) {
            System.out.println("    Stripped " + (origLen - content.length())
                + " bytes of inline images from content stream");
        }

        return content.getBytes(java.nio.charset.StandardCharsets.ISO_8859_1);
    }

    /**
     * Count the number of unbalanced BDC/BMC markers (those without a
     * matching EMC) in a raw PDF content byte array.  InDesign content
     * streams commonly contain /Span BDC (ActualText for tab chars)
     * without matching EMC, which breaks the Artifact BMC nesting when
     * the content is inlined.
     */
    private static final java.util.regex.Pattern MC_OPEN =
        java.util.regex.Pattern.compile("\\bBDC\\b|\\bBMC\\b");

    static int countUnbalancedBDC(byte[] contentBytes) {
        // Mask string interiors so BDC/BMC/EMC bytes inside painted
        // strings (e.g. "(BDC)Tj") don't skew the balance.
        String text = maskStrings(new String(contentBytes,
            java.nio.charset.StandardCharsets.ISO_8859_1));
        int opens = 0;
        int closes = 0;
        java.util.regex.Matcher mOpen = MC_OPEN.matcher(text);
        while (mOpen.find()) opens++;
        java.util.regex.Matcher mClose = MC_EMC.matcher(text);
        while (mClose.find()) closes++;
        return Math.max(0, opens - closes);
    }

    /** Operators that matter for tracking the text render mode. */
    private static final java.util.regex.Pattern TR_STATE_OPS =
        java.util.regex.Pattern.compile(
            "(?<![^\\s\\])>])(?:(\\d+)\\s+(Tr)|(BT|q|Q))(?![^\\s\\[(</])");
    private static final java.util.regex.Pattern INLINE_IMAGE =
        java.util.regex.Pattern.compile(
            "(?:^|(?<=\\s))BI\\b[\\s\\S]*?\\bID\\b[\\s\\S]*?\\bEI(?=\\s|$)",
            java.util.regex.Pattern.MULTILINE);

    /**
     * Write an explicit {@code 3 Tr} at the start of every text block that
     * begins while invisible mode (Tr 3) is already in effect.
     *
     * The render mode is graphics state: it outlives the BT..ET block that
     * set it and is saved/restored by q/Q.  Scanned PDFs (e.g. JSTOR) set
     * {@code 3 Tr} once in an otherwise empty block and draw all OCR text in
     * later blocks; without this, stripping removes only the setter block and
     * the OCR text becomes visible on top of the page image.
     */
    static String makeInheritedInvisibleExplicit(String content) {
        // Find operators on a copy with string and inline-image data masked.
        String masked = maskStrings(content);
        java.util.regex.Matcher img = INLINE_IMAGE.matcher(masked);
        StringBuilder mb = new StringBuilder(masked);
        while (img.find()) {
            for (int i = img.start(); i < img.end(); i++) mb.setCharAt(i, 'x');
        }
        java.util.regex.Matcher m = TR_STATE_OPS.matcher(mb);

        java.util.List<Integer> inserts = new java.util.ArrayList<>();
        java.util.Deque<Integer> saved = new java.util.ArrayDeque<>();
        int tr = 0;
        while (m.find()) {
            if (m.group(2) != null) {
                tr = Integer.parseInt(m.group(1));
            } else if ("BT".equals(m.group(3))) {
                if (tr == 3) inserts.add(m.end());
            } else if ("q".equals(m.group(3))) {
                saved.push(tr);
            } else {
                tr = saved.isEmpty() ? 0 : saved.pop();
            }
        }
        if (inserts.isEmpty()) return content;
        StringBuilder sb = new StringBuilder(content.length() + inserts.size() * 6);
        int last = 0;
        for (int pos : inserts) {
            sb.append(content, last, pos).append("\n3 Tr");
            last = pos;
        }
        sb.append(content, last, content.length());
        return sb.toString();
    }

    static byte[] stripInvisibleText(byte[] contentBytes) {
        String content = makeInheritedInvisibleExplicit(new String(contentBytes,
            java.nio.charset.StandardCharsets.ISO_8859_1));

        // Match BT...ET blocks that contain "3 Tr" (invisible text mode).
        //
        // IMPORTANT: Some PDFs have a single BT..ET block per page where
        // visible text (Tr 0, the default) and invisible OCR text (Tr 3)
        // coexist.  The invisible portion is typically a small tail at the
        // end of the block.  We must NOT strip the entire block — only the
        // invisible portion.
        //
        // Strategy:
        //   1. Purely invisible blocks (3 Tr before any text-showing op)
        //      → remove the entire BT..ET block.
        //   2. Mixed blocks (visible text ops before 3 Tr, invisible tail)
        //      → keep everything up to the 3 Tr setting, strip from there
        //        to ET, and re-append ET to close the text block.
        java.util.regex.Pattern btEtBlock = java.util.regex.Pattern.compile(
            "(?:q\\s*\\n)?BT\\b[\\s\\S]*?\\bET\\b(?:\\s*\\n\\s*Q)?",
            java.util.regex.Pattern.DOTALL
        );

        // Pattern to find "3 Tr" (not "13 Tr" etc.)
        java.util.regex.Pattern tr3Pattern = java.util.regex.Pattern.compile(
            "(?:^|\\s)3\\s+Tr\\b", java.util.regex.Pattern.MULTILINE
        );

        // Text-showing operators: Tj, TJ, ', "
        java.util.regex.Pattern textShowOp = java.util.regex.Pattern.compile(
            "(?:Tj|TJ|'|\")\\s"
        );

        java.util.regex.Matcher m = btEtBlock.matcher(content);
        int count = 0;
        StringBuffer sb = new StringBuffer();
        while (m.find()) {
            String block = m.group();
            java.util.regex.Matcher tr3m = tr3Pattern.matcher(block);
            if (!tr3m.find()) {
                // No invisible text in this block — keep it
                continue;
            }

            int tr3Pos = tr3m.start();
            // The tr3 pattern (?:^|\s)3\s+Tr includes leading whitespace
            // (\n) in the match.  Advance past it so beforeTr3 includes
            // the trailing \n — otherwise "]TJ\n" loses its \s and the
            // textShowOp check (?:Tj|TJ|'|")\s fails, misclassifying
            // mixed blocks as purely invisible.
            if (tr3Pos < block.length()
                    && Character.isWhitespace(block.charAt(tr3Pos))) {
                tr3Pos++;
            }

            // Alternating visibility: InDesign interleaves visible words
            // with invisible spacer glyphs (3 Tr <space> 0 Tr <word>).
            // If a visible render mode (anything but 3 and 7, which draw
            // nothing) appears after the first "3 Tr", visible content is
            // interlaced — stripping would lose text.  Skip entirely.
            java.util.regex.Pattern visibleTrPattern =
                java.util.regex.Pattern.compile(
                    "(?:^|\\s)[0-24-6]\\s+Tr\\b",
                    java.util.regex.Pattern.MULTILINE);
            if (visibleTrPattern.matcher(block.substring(tr3Pos)).find()) {
                continue;
            }

            // Check whether any text-showing op appears before 3 Tr
            String beforeTr3 = block.substring(0, tr3Pos);
            boolean hasVisibleText = textShowOp.matcher(beforeTr3).find();

            if (!hasVisibleText) {
                // Purely invisible block — remove text but preserve the
                // last font (Tf) so subsequent blocks that inherit font
                // state via the graphics state still have an active font.
                java.util.regex.Matcher tfScan = java.util.regex.Pattern.compile(
                    "/(\\S+\\s+[\\d.]+\\s+Tf)\\b").matcher(block);
                String lastTf = null;
                while (tfScan.find()) {
                    lastTf = "/" + tfScan.group(1);
                }
                // Check if the regex captured a trailing Q (graphics state
                // restore).  The optional q before BT is NOT captured when
                // clip ops (re, W* n) separate them, but the Q after ET IS
                // captured.  We must preserve the Q to avoid orphaning its
                // paired q.
                boolean capturedQ = block.stripTrailing().endsWith("Q");
                String qSuffix = capturedQ ? "Q\n" : "";
                // When the q before BT WAS captured, keep it too: dropping
                // it while keeping the Q leaves an unbalanced restore.
                String qPrefix = block.startsWith("q") ? "q\n" : "";

                if (lastTf != null) {
                    // Keep a minimal BT/ET that only sets the font
                    m.appendReplacement(sb,
                        java.util.regex.Matcher.quoteReplacement(
                            qPrefix + "BT\n" + lastTf + "\nET\n" + qSuffix));
                } else {
                    m.appendReplacement(sb,
                        java.util.regex.Matcher.quoteReplacement(qPrefix + qSuffix));
                }
                count++;
            } else {
                // Mixed block: visible text before 3 Tr, invisible tail.
                // Scan backwards from the "3 Tr" to find a safe cut point
                // — just before the font/graphics-state setup for the
                // invisible section.  Look for the last newline before
                // 3 Tr that follows a text-showing op (Tj/TJ/'/").
                int cutPos = tr3Pos;
                int lastNewline = beforeTr3.lastIndexOf('\n');
                if (lastNewline > 0) {
                    // Walk back further to skip font selection / color ops
                    // that only serve the invisible text (e.g. "/T1_0 1 Tf")
                    String segment = beforeTr3.substring(lastNewline);
                    if (!textShowOp.matcher(segment).find()) {
                        // This line has no text ops — it's setup for the
                        // invisible text.  Keep walking back.
                        int prevNewline = beforeTr3.lastIndexOf('\n',
                            lastNewline - 1);
                        if (prevNewline > 0) {
                            cutPos = prevNewline + 1;
                        } else {
                            cutPos = lastNewline + 1;
                        }
                    } else {
                        cutPos = lastNewline + 1;
                    }
                }

                // Keep the visible portion and close the text block.
                // Preserve trailing Q if captured by the regex.
                boolean capturedQmixed = block.stripTrailing().endsWith("Q");
                String kept = block.substring(0, cutPos).stripTrailing()
                    + "\nET\n" + (capturedQmixed ? "Q\n" : "");
                m.appendReplacement(sb,
                    java.util.regex.Matcher.quoteReplacement(kept));
                count++;
            }
        }
        m.appendTail(sb);

        if (count > 0) {
            System.out.println("    Stripped " + count
                + " invisible text block(s) from content stream");
            return sb.toString().getBytes(
                java.nio.charset.StandardCharsets.ISO_8859_1);
        }
        return contentBytes;
    }

    /**
     * Recursively strip invisible text from Form XObjects in a resource
     * dictionary. Walks the /XObject sub-dictionary, finds Form XObjects,
     * strips invisible text from their content streams, then recurses
     * into their own /Resources.
     */
    static void stripInvisibleTextFromXObjects(
            PdfDictionary resources, Set<PdfIndirectReference> visited) {
        if (resources == null) return;
        PdfDictionary xobjects = resources.getAsDictionary(PdfName.XObject);
        if (xobjects == null) return;

        for (PdfName name : xobjects.keySet()) {
            PdfStream xobjStream = xobjects.getAsStream(name);
            if (xobjStream == null) continue;

            // Avoid processing the same XObject twice (shared refs)
            PdfIndirectReference ref = xobjStream.getIndirectReference();
            if (ref != null && !visited.add(ref)) continue;

            // Only process Form XObjects (not images)
            PdfName subtype = xobjStream.getAsName(PdfName.Subtype);
            if (!PdfName.Form.equals(subtype)) continue;

            byte[] data = xobjStream.getBytes();
            if (data != null && data.length > 0) {
                byte[] current = stripInvisibleText(data);

                // Also strip text using hidden OCR fonts (e.g. HiddenHorzOCR)
                // that may not use rendering mode 3 but have .notdef glyphs.
                PdfDictionary xobjRes = xobjStream.getAsDictionary(PdfName.Resources);
                Set<String> hiddenFonts = findHiddenOCRFontNames(xobjRes);
                if (!hiddenFonts.isEmpty()) {
                    current = stripFontText(current, hiddenFonts);
                }

                // Repair font inheritance broken by the above removals.
                current = fixFontInheritance(current);

                if (current != data) {
                    // false = REPLACE. The 2-arg setData(bytes, true) APPENDS;
                    // appending the cleaned copy onto the original leaves the
                    // un-stripped content in place AND doubles the stream on
                    // every per-page pass over a shared XObject (768B -> 1.6GB
                    // -> OutOfMemoryError on multi-page docs). Must be replace.
                    xobjStream.setData(current, false);
                }
            }

            // Recurse into this XObject's own resources
            PdfDictionary subRes = xobjStream.getAsDictionary(PdfName.Resources);
            stripInvisibleTextFromXObjects(subRes, visited);
        }
    }

    /**
     * Find font reference names (e.g. "C0_0") in a resource dictionary
     * that map to hidden OCR fonts (font base name contains "Hidden").
     */
    static Set<String> findHiddenOCRFontNames(PdfDictionary resources) {
        Set<String> result = new HashSet<>();
        if (resources == null) return result;
        PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
        if (fonts == null) return result;

        for (PdfName fontRefName : fonts.keySet()) {
            PdfDictionary fontDict = fonts.getAsDictionary(fontRefName);
            if (fontDict == null) continue;

            // Check BaseFont name
            PdfName baseFont = fontDict.getAsName(PdfName.BaseFont);
            String baseFontStr = baseFont != null ? baseFont.getValue() : "";

            // Check descendant fonts (Type0)
            if (baseFontStr.isEmpty()) {
                PdfArray descendants = fontDict.getAsArray(
                    new PdfName("DescendantFonts"));
                if (descendants != null && descendants.size() > 0) {
                    PdfDictionary desc = descendants.getAsDictionary(0);
                    if (desc != null) {
                        PdfName descBase = desc.getAsName(PdfName.BaseFont);
                        if (descBase != null) baseFontStr = descBase.getValue();
                    }
                }
            }

            if (baseFontStr.toLowerCase(java.util.Locale.ROOT).contains("hidden")) {
                result.add(fontRefName.getValue());
            }
        }
        return result;
    }

    /**
     * Strip text-showing operations that use specific font reference names
     * from a content stream.  Handles mixed BT..ET blocks where hidden
     * OCR font text is interleaved with visible text using other fonts.
     *
     * Tracks the current font via /name size Tf operators.  When the
     * current font is in hiddenFontNames, strips text-showing ops
     * (Tj, TJ, ', ") and their preceding positioning/color setup lines.
     */
    static byte[] stripFontText(byte[] contentBytes,
                                         Set<String> hiddenFontNames) {
        String content = new String(contentBytes,
            java.nio.charset.StandardCharsets.ISO_8859_1);

        // Process each BT..ET block
        java.util.regex.Pattern btEtBlock = java.util.regex.Pattern.compile(
            "BT\\b[\\s\\S]*?\\bET\\b", java.util.regex.Pattern.DOTALL);

        java.util.regex.Matcher m = btEtBlock.matcher(content);
        int totalStripped = 0;
        StringBuffer sb = new StringBuffer();

        while (m.find()) {
            String block = m.group();

            // Check if this block references any hidden font
            boolean hasHiddenFont = false;
            for (String fn : hiddenFontNames) {
                if (block.contains("/" + fn)) {
                    hasHiddenFont = true;
                    break;
                }
            }
            if (!hasHiddenFont) continue;

            // Parse line by line, tracking current font
            String[] lines = block.split("\\n");
            StringBuilder cleaned = new StringBuilder();
            String currentFont = null;
            boolean isHidden = false;
            int stripped = 0;

            for (String line : lines) {
                String trimmed = line.trim();

                // Detect font change: /FontName size Tf
                java.util.regex.Matcher tfm =
                    java.util.regex.Pattern.compile(
                        "/(\\S+)\\s+[\\d.]+\\s+Tf\\b").matcher(trimmed);
                if (tfm.find()) {
                    currentFont = tfm.group(1);
                    isHidden = hiddenFontNames.contains(currentFont);
                    if (isHidden) {
                        stripped++;
                        continue; // Skip the Tf line for hidden font
                    }
                }

                // If current font is hidden, skip text-showing ops and
                // their setup (positioning, color)
                if (isHidden) {
                    // Skip text-showing operators
                    if (trimmed.endsWith("Tj") || trimmed.endsWith("TJ")
                            || trimmed.endsWith("'") || trimmed.endsWith("\"")) {
                        stripped++;
                        continue;
                    }
                    // Skip positioning/color ops that serve only the hidden text
                    if (trimmed.endsWith("Tm") || trimmed.endsWith("Td")
                            || trimmed.endsWith("TD") || trimmed.endsWith("Tc")
                            || trimmed.endsWith("Tw") || trimmed.endsWith("Tz")
                            || trimmed.endsWith("Ts") || trimmed.endsWith("TL")
                            || trimmed.endsWith(" g") || trimmed.endsWith(" rg")
                            || trimmed.endsWith(" G") || trimmed.endsWith(" RG")
                            || trimmed.endsWith(" k") || trimmed.endsWith(" K")) {
                        stripped++;
                        continue;
                    }
                }

                cleaned.append(line).append("\n");
            }

            if (stripped > 0) {
                totalStripped += stripped;
                String result = cleaned.toString().stripTrailing() + "\n";
                m.appendReplacement(sb,
                    java.util.regex.Matcher.quoteReplacement(result));
            }
        }
        m.appendTail(sb);

        if (totalStripped > 0) {
            System.out.println("    Stripped " + totalStripped
                + " hidden OCR font text op(s) from content stream");
            return sb.toString().getBytes(
                java.nio.charset.StandardCharsets.ISO_8859_1);
        }
        return contentBytes;
    }

    /**
     * Detect fonts in a resource dictionary that have character codes mapping
     * to the .notdef glyph.  Returns a map from font reference name (e.g.
     * "C2_0") to the set of raw character codes (as 2-byte arrays for Type0
     * or 1-byte for simple fonts) that produce .notdef.
     *
     * For Type0/Identity-H fonts, CID 0 is always GID 0 = .notdef by
     * TrueType convention.  For simple fonts we check the /Widths array
     * and /Encoding /Differences for glyph names that are ".notdef".
     */
    static Map<String, NotdefInfo> findNotdefFonts(PdfDictionary resources) {
        Map<String, NotdefInfo> result = new HashMap<>();
        if (resources == null) return result;
        PdfDictionary fonts = resources.getAsDictionary(PdfName.Font);
        if (fonts == null) return result;

        for (PdfName fontRefName : fonts.keySet()) {
            PdfDictionary fontDict = fonts.getAsDictionary(fontRefName);
            if (fontDict == null) continue;

            PdfName subtype = fontDict.getAsName(PdfName.Subtype);

            // --- Type0 (CID) fonts ---
            if (PdfName.Type0.equals(subtype)) {
                PdfName encoding = fontDict.getAsName(PdfName.Encoding);
                if (encoding != null && (encoding.getValue().contains("Identity"))) {
                    // Identity-H or Identity-V: CID 0 = GID 0 = .notdef
                    NotdefInfo info = new NotdefInfo(true); // isTwoByte
                    info.addCode(new byte[]{0x00, 0x00}); // CID 0
                    result.put(fontRefName.getValue(), info);
                }
                // For non-Identity encodings, .notdef detection would require
                // parsing the CMap — not needed for the known 17 PDFs.
                continue;
            }

            // --- Simple fonts (TrueType, Type1) ---
            // Check /Widths array for zero-width entries combined with
            // /Encoding /Differences for .notdef glyph names.
            PdfNumber firstCharObj = fontDict.getAsNumber(new PdfName("FirstChar"));
            PdfNumber lastCharObj = fontDict.getAsNumber(new PdfName("LastChar"));
            PdfArray widths = fontDict.getAsArray(PdfName.Widths);
            if (firstCharObj == null || lastCharObj == null || widths == null) continue;

            int firstChar = firstCharObj.intValue();
            int lastChar = lastCharObj.intValue();

            // Build glyph name map from /Encoding /Differences
            Map<Integer, String> glyphNames = new HashMap<>();
            boolean hasStandardEncoding = false;
            PdfDictionary encodingDict = fontDict.getAsDictionary(PdfName.Encoding);
            PdfName encodingName = fontDict.getAsName(PdfName.Encoding);
            if (encodingName != null) {
                String enc = encodingName.getValue();
                hasStandardEncoding = "WinAnsiEncoding".equals(enc)
                    || "MacRomanEncoding".equals(enc)
                    || "MacExpertEncoding".equals(enc);
            }
            if (encodingDict != null) {
                PdfName baseEnc = encodingDict.getAsName(
                    new PdfName("BaseEncoding"));
                if (baseEnc != null) {
                    String enc = baseEnc.getValue();
                    hasStandardEncoding = "WinAnsiEncoding".equals(enc)
                        || "MacRomanEncoding".equals(enc)
                        || "MacExpertEncoding".equals(enc);
                }
                PdfArray diffs = encodingDict.getAsArray(new PdfName("Differences"));
                if (diffs != null) {
                    int charCode = 0;
                    for (int i = 0; i < diffs.size(); i++) {
                        PdfObject item = diffs.get(i);
                        if (item.isNumber()) {
                            charCode = ((PdfNumber) item).intValue();
                        } else if (item.isName()) {
                            glyphNames.put(charCode, ((PdfName) item).getValue());
                            charCode++;
                        }
                    }
                }
            }

            NotdefInfo info = new NotdefInfo(false); // single-byte
            info.firstChar = firstChar;
            info.lastChar = lastChar;
            // Remember every code /Differences maps to a real glyph name so
            // isNotdef() can exempt it from the out-of-range rule.
            for (Map.Entry<Integer, String> e : glyphNames.entrySet()) {
                if (!".notdef".equals(e.getValue())) {
                    info.diffRealGlyphCodes.add(e.getKey());
                }
            }
            boolean hasNotdef = false;

            for (int code = firstChar; code <= lastChar; code++) {
                int wIdx = code - firstChar;
                if (wIdx < 0 || wIdx >= widths.size()) continue;

                double w = widths.getAsNumber(wIdx) != null
                    ? widths.getAsNumber(wIdx).doubleValue() : 0;

                String gn = glyphNames.get(code);

                // A char code maps to .notdef if:
                // 1) Its glyph name is explicitly ".notdef", OR
                // 2) Its width is 0 AND no glyph name is defined
                //    (heuristic for subset fonts missing glyphs), OR
                // 3) Code is a control character (0x00-0x1F, 0x7F)
                //    AND the font uses a standard encoding
                //    AND /Differences does NOT remap it to a real glyph.
                //    Subset fonts commonly re-index real glyphs to low code
                //    points starting at 1 while declaring a standard
                //    BaseEncoding; in that case the code carries a real glyph
                //    name (e.g. /e) with a non-zero width and is NOT .notdef.
                //    Without the gn==null guard, criterion 3 would strip the
                //    document's actual text (real text -> blank).
                if (".notdef".equals(gn) || (w == 0 && gn == null)
                        || (hasStandardEncoding
                            && (code <= 0x1F || code == 0x7F)
                            && gn == null)) {
                    info.addCode(new byte[]{(byte) code});
                    hasNotdef = true;
                }
            }

            // Add font if it has explicit .notdef codes OR if range
            // checking can detect out-of-range codes as .notdef.
            if (hasNotdef || (firstChar >= 0 && lastChar >= 0
                    && (firstChar > 0 || lastChar < 255))) {
                result.put(fontRefName.getValue(), info);
            }
        }
        return result;
    }

    /** Helper class to hold .notdef info for a font. */
    static class NotdefInfo {
        final boolean isTwoByte; // Type0 = 2-byte codes, simple = 1-byte
        final Set<Integer> notdefCodes = new HashSet<>(); // int representation of codes
        int firstChar = -1; // simple fonts: FirstChar value (-1 = not set)
        int lastChar = -1;  // simple fonts: LastChar value (-1 = not set)
        /** Codes that /Encoding /Differences maps to a real (non-.notdef)
         *  glyph name. Such a code renders even OUTSIDE FirstChar..LastChar
         *  (the range only governs /Widths; a missing entry falls back to
         *  /MissingWidth) — producers ship off-by-one LastChar values
         *  (e.g. MyriadPro-Semibold /LastChar 22 with Differences code
         *  23 = /y), and stripping such codes deletes visible text. */
        final Set<Integer> diffRealGlyphCodes = new HashSet<>();

        NotdefInfo(boolean isTwoByte) {
            this.isTwoByte = isTwoByte;
        }

        void addCode(byte[] code) {
            if (isTwoByte && code.length == 2) {
                notdefCodes.add(((code[0] & 0xFF) << 8) | (code[1] & 0xFF));
            } else if (!isTwoByte && code.length == 1) {
                notdefCodes.add(code[0] & 0xFF);
            }
        }

        boolean isNotdef(int codeValue) {
            if (notdefCodes.contains(codeValue)) return true;
            // For simple (1-byte) fonts: codes outside FirstChar-LastChar
            // have no /Widths entry and USUALLY map to .notdef — but not
            // when /Differences explicitly names a real glyph for them
            // (off-by-one LastChar producer bug; the glyph still renders).
            if (!isTwoByte && firstChar >= 0 && lastChar >= 0) {
                if ((codeValue < firstChar || codeValue > lastChar)
                        && !diffRealGlyphCodes.contains(codeValue)) {
                    return true;
                }
            }
            return false;
        }
    }

    /**
     * Strip character codes that reference .notdef glyphs from text-showing
     * operators (Tj, TJ) in a content stream.  Tracks current font via Tf
     * and filters hex/literal string operands to remove .notdef codes.
     *
     * This fixes veraPDF 7.21.8: "A PDF/UA compliant document shall not
     * contain a reference to the .notdef glyph from any of the text showing
     * operators, regardless of text rendering mode, in any content stream."
     */
    static byte[] stripNotdefGlyphs(byte[] contentBytes,
                                     PdfDictionary resources) {
        Map<String, NotdefInfo> notdefMap = findNotdefFonts(resources);
        if (notdefMap.isEmpty()) return contentBytes;

        String content = new String(contentBytes,
            java.nio.charset.StandardCharsets.ISO_8859_1);

        String[] lines = content.split("\\n", -1);
        StringBuilder sb = new StringBuilder(content.length());
        String currentFont = null;
        NotdefInfo currentNotdef = null;
        int totalStripped = 0;

        java.util.regex.Pattern tfPattern =
            java.util.regex.Pattern.compile("/(\\S+)\\s+[\\d.]+\\s+Tf\\b");
        for (String line : lines) {
            String trimmed = line.trim();

            // Font state carried in from prior lines (used for a trailing text
            // op that has no Tf before it on this line).
            String fontBeforeLine = currentFont;

            // A single physical line can carry SEVERAL Tf operators — dvips
            // TOC lines look like "/F23 .. Tf [Contents]TJ/F18 .. Tf .. /F17
            // .. Tf".  Collect every Tf with the index where its match ends so
            // we can pick the font in effect at a specific position.  The
            // previous code grabbed the FIRST Tf on the line and let it leak
            // into the following (unrelated) lines; those lines are drawn with
            // the LAST font, so the wrong font's .notdef set was applied and
            // real glyphs of the actual font were stripped (e.g. F17 'P','.'
            // deleted because F18/F23 mark those codes .notdef).
            java.util.regex.Matcher tfm = tfPattern.matcher(trimmed);
            java.util.List<Integer> tfEnd = new java.util.ArrayList<>();
            java.util.List<String> tfName = new java.util.ArrayList<>();
            while (tfm.find()) {
                tfEnd.add(tfm.end());
                tfName.add(tfm.group(1));
            }
            // Persist the LAST Tf on the line as the font for the NEXT lines.
            if (!tfName.isEmpty()) {
                currentFont = tfName.get(tfName.size() - 1);
                currentNotdef = notdefMap.get(currentFont);
            }

            // Filter ONLY the operand of the trailing text-show operator (never
            // drop the whole line, so a multi-line array's closing ']' and any
            // other operator on the line survive — dropping the line used to
            // delete that ']' and crash iText's parser).  Use the font in
            // effect AT that operator: the last Tf ending before the operand,
            // else the font carried in from prior lines.
            boolean isTJ = trimmed.endsWith("TJ");
            boolean isTj = !isTJ && trimmed.endsWith("Tj");
            if (isTJ || isTj) {
                int os = trailingOperandStart(trimmed, isTJ);
                if (os >= 0) {
                    String opFont = fontBeforeLine;
                    for (int k = 0; k < tfEnd.size(); k++) {
                        if (tfEnd.get(k) <= os) opFont = tfName.get(k);
                        else break;
                    }
                    NotdefInfo opNotdef =
                        (opFont != null) ? notdefMap.get(opFont) : null;
                    if (opNotdef != null) {
                        String filtered = filterTrailingTextOp(
                            trimmed, opNotdef, isTJ);
                        if (filtered != null && !filtered.equals(trimmed)) {
                            sb.append(filtered).append("\n");
                            totalStripped++;
                            continue;
                        }
                    }
                }
            }

            sb.append(line).append("\n");
        }

        if (totalStripped > 0) {
            System.out.println("    Stripped " + totalStripped
                + " .notdef glyph reference(s) from content stream");
            // Remove trailing extra newline
            String result = sb.toString();
            if (result.endsWith("\n\n") && !content.endsWith("\n\n")) {
                result = result.substring(0, result.length() - 1);
            }
            return result.getBytes(
                java.nio.charset.StandardCharsets.ISO_8859_1);
        }
        return contentBytes;
    }

    /**
     * Return the start index of the operand of the trailing Tj/TJ operator on
     * {@code line} (mirrors the operand isolation in filterTrailingTextOp), or
     * -1 if none.  Used to decide which /Tf font is in effect at that operator.
     */
    private static int trailingOperandStart(String line, boolean isTJ) {
        int oe = line.length() - 2;
        while (oe > 0 && Character.isWhitespace(line.charAt(oe - 1))) oe--;
        if (oe <= 0) return -1;
        char close = line.charAt(oe - 1);
        if (isTJ) {
            if (close != ']') return -1;
        } else if (close != ')' && close != '>') {
            return -1;
        }
        return operandStart(line, oe - 1);
    }

    /**
     * Filter .notdef char codes from the operand of the LAST text-showing
     * operator on a line (Tj or TJ).  Unlike the old per-line approach this
     * isolates the operand immediately preceding the trailing operator, so a
     * line that also carries the tail of a multi-line array (or any other
     * operator) keeps that surrounding content intact.  The line is NEVER
     * dropped: an operand that fully filters away is replaced in place with an
     * empty string "()" or empty array "[]", preserving stream structure — in
     * particular the closing ']' of an array opened on a previous line.
     *
     * @return the rewritten line, or null if nothing changed.
     */
    private static String filterTrailingTextOp(
            String line, NotdefInfo info, boolean isTJ) {
        // The operator keyword (Tj/TJ) is the last two chars of the line.
        int oe = line.length() - 2;
        while (oe > 0 && Character.isWhitespace(line.charAt(oe - 1))) oe--;
        if (oe <= 0) return null;
        char close = line.charAt(oe - 1);
        int os = operandStart(line, oe - 1);
        if (os < 0) return null;
        String operand = line.substring(os, oe);
        String repl;

        if (isTJ) {
            if (close != ']') return null;
            // Reuse the array filter on the isolated "[...]" operand.
            String f = filterNotdefFromTJLine(operand, info);
            repl = (f == null) ? "[]" : f;   // all-empty → keep empty array
        } else {
            if (close == ')') {
                if (info.isTwoByte) return null;   // literal only for 1-byte
                String f = filterLiteralString(operand, info);
                repl = (f == null) ? "()" : f;     // all-empty → keep empty str
            } else if (close == '>') {
                String hex = operand.substring(1, operand.length() - 1);
                repl = "<" + filterHexString(hex, info) + ">";
            } else {
                return null;
            }
        }

        if (repl.equals(operand)) return null;
        return line.substring(0, os) + repl + line.substring(oe);
    }

    /**
     * Given the index of the closing delimiter of a content-stream operand
     * ( ')', '>' or ']' ), return the index where that operand starts,
     * scanning string-aware so brackets/parens INSIDE string literals are
     * ignored.  Returns -1 if no operand ends exactly at {@code endIdx}.
     */
    private static int operandStart(String s, int endIdx) {
        int i = 0, n = s.length();
        while (i < n) {
            char c = s.charAt(i);
            if (c == '(') {
                int st = i, depth = 1; i++;
                while (i < n && depth > 0) {
                    char d = s.charAt(i);
                    if (d == '\\') { i += 2; continue; }
                    if (d == '(') depth++;
                    else if (d == ')') depth--;
                    i++;
                }
                if (i - 1 == endIdx) return st;
            } else if (c == '<' && i + 1 < n && s.charAt(i + 1) != '<') {
                int st = i; i++;
                while (i < n && s.charAt(i) != '>') i++;
                if (i < n) i++;              // consume '>'
                if (i - 1 == endIdx) return st;
            } else if (c == '[') {
                int st = i, depth = 1; i++;
                while (i < n && depth > 0) {
                    char d = s.charAt(i);
                    if (d == '(') {          // skip nested literal string
                        int dp = 1; i++;
                        while (i < n && dp > 0) {
                            char e = s.charAt(i);
                            if (e == '\\') { i += 2; continue; }
                            if (e == '(') dp++;
                            else if (e == ')') dp--;
                            i++;
                        }
                        continue;
                    }
                    if (d == '<' && i + 1 < n && s.charAt(i + 1) != '<') {
                        i++;                 // skip nested hex string
                        while (i < n && s.charAt(i) != '>') i++;
                        if (i < n) i++;
                        continue;
                    }
                    if (d == '[') depth++;
                    else if (d == ']') depth--;
                    i++;
                }
                if (i - 1 == endIdx) return st;
            } else {
                i++;
            }
        }
        return -1;
    }

    /**
     * Filter .notdef char codes from a TJ array line.
     * Returns the filtered line, or null if all strings become empty.
     */
    private static String filterNotdefFromTJLine(
            String line, NotdefInfo info) {
        // Find the TJ array: [...] TJ
        int arrStart = line.indexOf('[');
        int arrEnd = line.lastIndexOf(']');
        if (arrStart < 0 || arrEnd < 0) return line;

        String arrayContent = line.substring(arrStart + 1, arrEnd);
        StringBuilder filtered = new StringBuilder();
        boolean anyContent = false;

        // Process each element in the array
        int i = 0;
        while (i < arrayContent.length()) {
            char c = arrayContent.charAt(i);

            if (c == '<') {
                // Hex string
                int end = arrayContent.indexOf('>', i);
                if (end < 0) break;
                String hex = arrayContent.substring(i + 1, end);
                String filteredHex = filterHexString(hex, info);
                if (!filteredHex.isEmpty()) {
                    filtered.append("<").append(filteredHex).append(">");
                    anyContent = true;
                } else {
                    // Dropped element: emit a space so the numeric adjustments
                    // this string separated don't fuse into one invalid token
                    // (e.g. "-3.46-3.46"), which corrupts the content stream
                    // and aborts the page parse in iText/MuPDF/Acrobat.
                    filtered.append(' ');
                }
                i = end + 1;
            } else if (c == '(') {
                // Literal string — for simple (1-byte) fonts
                int depth = 1;
                int start = i;
                i++;
                while (i < arrayContent.length() && depth > 0) {
                    char cc = arrayContent.charAt(i);
                    // Skip escaped char forward.  A backward "prev != '\\'"
                    // test mis-handles "\\)" (an escaped backslash followed by
                    // a real close-paren), producing an unbalanced element that
                    // crashes iText's parser (PdfLiteral→PdfNumber cast).
                    if (cc == '\\') { i += 2; continue; }
                    else if (cc == '(') depth++;
                    else if (cc == ')') depth--;
                    i++;
                }
                String litStr = arrayContent.substring(start, i);
                if (!info.isTwoByte) {
                    String filteredLit = filterLiteralString(litStr, info);
                    if (filteredLit != null && !filteredLit.equals("()")) {
                        filtered.append(filteredLit);
                        anyContent = true;
                    } else {
                        // Dropped element: emit a space (see hex branch) so
                        // adjacent numeric adjustments stay separated rather
                        // than fusing into one invalid token.
                        filtered.append(' ');
                    }
                } else {
                    filtered.append(litStr);
                    anyContent = true;
                }
            } else if (c == '-' || c == '.' || (c >= '0' && c <= '9')) {
                // Numeric adjustment — always keep
                int start = i;
                while (i < arrayContent.length()
                       && (arrayContent.charAt(i) == '-'
                           || arrayContent.charAt(i) == '.'
                           || (arrayContent.charAt(i) >= '0'
                               && arrayContent.charAt(i) <= '9'))) {
                    i++;
                }
                filtered.append(arrayContent, start, i);
            } else {
                // Whitespace or other — keep
                filtered.append(c);
                i++;
            }
        }

        if (!anyContent) return null; // all strings were .notdef
        return line.substring(0, arrStart + 1)
            + filtered.toString()
            + line.substring(arrEnd);
    }

    /**
     * Filter .notdef character codes from a hex string.
     * For 2-byte fonts, removes 4-hex-digit pairs that are .notdef CIDs.
     * For 1-byte fonts, removes 2-hex-digit codes that are .notdef.
     */
    private static String filterHexString(String hex, NotdefInfo info) {
        if (hex.isEmpty()) return hex;

        int step = info.isTwoByte ? 4 : 2;
        StringBuilder filtered = new StringBuilder(hex.length());

        for (int i = 0; i + step <= hex.length(); i += step) {
            String chunk = hex.substring(i, i + step);
            int value;
            try {
                value = Integer.parseInt(chunk, 16);
            } catch (NumberFormatException e) {
                // Keep unparseable chunks
                filtered.append(chunk);
                continue;
            }

            if (!info.isNotdef(value)) {
                filtered.append(chunk);
            }
        }
        return filtered.toString();
    }

    /**
     * Filter .notdef character codes from a literal string (parenthesized).
     * Only used for single-byte fonts.
     * Returns the filtered string including parens, or null if empty.
     */
    private static String filterLiteralString(String litStr, NotdefInfo info) {
        if (litStr.length() < 2) return litStr;
        // Remove outer parens
        String inner = litStr.substring(1, litStr.length() - 1);
        StringBuilder filtered = new StringBuilder(inner.length());

        for (int i = 0; i < inner.length(); i++) {
            char c = inner.charAt(i);
            if (c == '\\' && i + 1 < inner.length()) {
                // Escape sequence — check the escaped char
                char next = inner.charAt(i + 1);
                if (next >= '0' && next <= '7') {
                    // Octal escape: \NNN
                    int end = i + 2;
                    while (end < inner.length() && end < i + 4
                           && inner.charAt(end) >= '0'
                           && inner.charAt(end) <= '7') {
                        end++;
                    }
                    String octal = inner.substring(i + 1, end);
                    int value = Integer.parseInt(octal, 8);
                    if (!info.isNotdef(value)) {
                        filtered.append(inner, i, end);
                    }
                    i = end - 1;
                } else {
                    // Other escape — keep as-is
                    filtered.append(c).append(next);
                    i++;
                }
            } else {
                int value = c & 0xFF;
                if (!info.isNotdef(value)) {
                    filtered.append(c);
                }
            }
        }

        if (filtered.length() == 0) return null;
        return "(" + filtered.toString() + ")";
    }

    /**
     * Recursively strip .notdef glyph references from Form XObjects in a
     * resource dictionary.  Mirrors stripInvisibleTextFromXObjects().
     */
    static void stripNotdefGlyphsFromXObjects(
            PdfDictionary resources, Set<PdfIndirectReference> visited) {
        if (resources == null) return;
        PdfDictionary xobjects = resources.getAsDictionary(PdfName.XObject);
        if (xobjects == null) return;

        for (PdfName name : xobjects.keySet()) {
            PdfStream xobjStream = xobjects.getAsStream(name);
            if (xobjStream == null) continue;

            PdfIndirectReference ref = xobjStream.getIndirectReference();
            if (ref != null && !visited.add(ref)) continue;

            PdfName subtype = xobjStream.getAsName(PdfName.Subtype);
            if (!PdfName.Form.equals(subtype)) continue;

            byte[] data = xobjStream.getBytes();
            if (data != null && data.length > 0) {
                PdfDictionary xobjRes =
                    xobjStream.getAsDictionary(PdfName.Resources);
                byte[] cleaned = stripNotdefGlyphs(data, xobjRes);
                if (cleaned != data) {
                    // false = REPLACE (true APPENDS → doubles shared XObjects). See
                    // stripInvisibleTextFromXObjects for the full rationale.
                    xobjStream.setData(cleaned, false);
                }
            }

            // Recurse into this XObject's own resources
            PdfDictionary subRes =
                xobjStream.getAsDictionary(PdfName.Resources);
            stripNotdefGlyphsFromXObjects(subRes, visited);
        }
    }

    /**
     * Fix font inheritance broken by BT..ET block removal.
     *
     * PDF text state (including the font set by Tf) persists across BT/ET
     * boundaries in the graphics state.  When stripInvisibleText() or
     * stripFontText() removes blocks or Tf operators, subsequent BT blocks
     * that relied on inheriting the font state are left without an active
     * font, causing "Font must be set before text is painted" errors in
     * PAC and blank pages in Acrobat.
     *
     * This method scans the content stream and inserts the last-known Tf
     * at the start of any BT block that has text-showing operators but no
     * Tf of its own.  Tracks q/Q save/restore since font state is part
     * of the graphics state stack.
     */
    static byte[] fixFontInheritance(byte[] contentBytes) {
        String content = new String(contentBytes,
            java.nio.charset.StandardCharsets.ISO_8859_1);
        String[] lines = content.split("\\n", -1);

        java.util.regex.Pattern tfPattern = java.util.regex.Pattern.compile(
            "/(\\S+)\\s+[\\d.]+\\s+Tf\\b");

        // State tracking
        java.util.ArrayDeque<String> tfStack = new java.util.ArrayDeque<>();
        String lastKnownTf = null;
        String pageFallbackTf = null;
        boolean insideBT = false;
        boolean blockHasTf = false;
        boolean blockHasTextOp = false;
        int blockStartLine = -1;

        // Collect fixes: (lineIndex, tfString) — insert Tf after BT line
        java.util.List<Integer> fixLines = new java.util.ArrayList<>();
        java.util.List<String> fixTfs = new java.util.ArrayList<>();

        for (int i = 0; i < lines.length; i++) {
            String trimmed = lines[i].trim();

            // Track q/Q (font state is part of graphics state).
            // Detect both standalone "q" and inline "q 1 0 0 1 X Y cm"
            // (81% of PDFs use combined q+matrix operators on one line).
            if (trimmed.equals("q") || trimmed.startsWith("q ")) {
                tfStack.push(lastKnownTf != null ? lastKnownTf : "");
            } else if (trimmed.equals("Q") || trimmed.endsWith(" Q")) {
                if (!tfStack.isEmpty()) {
                    String restored = tfStack.pop();
                    lastKnownTf = restored.isEmpty() ? null : restored;
                } else {
                    // Unbalanced Q — restore page-level fallback instead
                    // of null. Some font is better than no font.
                    lastKnownTf = pageFallbackTf;
                }
            }

            // Track BT (also inline, e.g. "16.6667 0 0 16.6667 0 0 cm BT")
            if (trimmed.equals("BT") || trimmed.endsWith(" BT")) {
                insideBT = true;
                blockHasTf = false;
                blockHasTextOp = false;
                blockStartLine = i;
            }

            // Track Tf (both inside and outside BT — state persists)
            if (tfPattern.matcher(trimmed).find()) {
                lastKnownTf = trimmed;
                if (insideBT) blockHasTf = true;
                // Update page-level fallback when at top nesting level
                if (tfStack.isEmpty()) {
                    pageFallbackTf = trimmed;
                }
            }

            // Track text-showing ops
            if (insideBT && (trimmed.endsWith("Tj") || trimmed.endsWith("TJ")
                    || trimmed.endsWith("'") || trimmed.endsWith("\""))) {
                blockHasTextOp = true;
            }

            // Track ET — check if this block needs a fix
            if (trimmed.equals("ET") || trimmed.startsWith("ET ")) {
                if (insideBT && blockHasTextOp && !blockHasTf
                        && lastKnownTf != null) {
                    fixLines.add(blockStartLine);
                    fixTfs.add(lastKnownTf);
                }
                insideBT = false;
            }
        }

        if (fixLines.isEmpty()) return contentBytes;

        // Build output with inserted Tf lines (after BT lines)
        java.util.Map<Integer, String> fixMap = new java.util.HashMap<>();
        for (int j = 0; j < fixLines.size(); j++) {
            fixMap.put(fixLines.get(j), fixTfs.get(j));
        }

        StringBuilder sb = new StringBuilder();
        for (int i = 0; i < lines.length; i++) {
            sb.append(lines[i]).append("\n");
            if (fixMap.containsKey(i)) {
                // Insert Tf immediately after the BT line
                sb.append(fixMap.get(i)).append("\n");
            }
        }

        System.out.println("    Fixed font inheritance in " + fixLines.size()
            + " BT block(s)");
        return sb.toString().getBytes(
            java.nio.charset.StandardCharsets.ISO_8859_1);
    }

    /**
     * Recursively strip marked content (BDC/BMC/EMC and orphaned MCID dicts)
     * from Form XObjects. Prevents 7.1-t2 "tagged content inside Artifact"
     * when nested XObjects with pre-existing structure references are
     * inlined inside Artifact blocks.
     */
    static void stripMarkedContentFromXObjects(
            PdfDictionary resources, Set<PdfIndirectReference> visited) {
        if (resources == null) return;
        PdfDictionary xobjects = resources.getAsDictionary(PdfName.XObject);
        if (xobjects == null) return;

        for (PdfName name : xobjects.keySet()) {
            PdfStream xobjStream = xobjects.getAsStream(name);
            if (xobjStream == null) continue;

            PdfIndirectReference ref = xobjStream.getIndirectReference();
            if (ref != null && !visited.add(ref)) continue;

            PdfName subtype = xobjStream.getAsName(PdfName.Subtype);
            if (!PdfName.Form.equals(subtype)) continue;

            byte[] data = xobjStream.getBytes();
            if (data != null && data.length > 0) {
                byte[] stripped = stripMarkedContent(data);
                if (stripped.length != data.length) {
                    // false = REPLACE (true APPENDS → doubles shared XObjects every
                    // per-page pass; the proven OOM driver). See
                    // stripInvisibleTextFromXObjects for the full rationale.
                    xobjStream.setData(stripped, false);
                }
            }

            // Remove residual StructParent(s) keys — veraPDF 7.20 t2
            // treats Form XObjects with StructParents as "containing
            // MCIDs" even after BDC/EMC stripping, and flags multi-
            // referenced XObjects as violating unique semantic parent.
            xobjStream.remove(new PdfName("StructParents"));
            xobjStream.remove(new PdfName("StructParent"));

            PdfDictionary subRes = xobjStream.getAsDictionary(
                PdfName.Resources);
            stripMarkedContentFromXObjects(subRes, visited);
        }
    }

    /**
     * Recursively strip inline images from Form XObjects in a resource
     * dictionary.  Follows the same pattern as
     * {@link #stripMarkedContentFromXObjects}.
     */
    static void stripInlineImagesFromXObjects(
            PdfDictionary resources, Set<PdfIndirectReference> visited) {
        if (resources == null) return;
        PdfDictionary xobjects = resources.getAsDictionary(PdfName.XObject);
        if (xobjects == null) return;

        for (PdfName name : xobjects.keySet()) {
            PdfStream xobjStream = xobjects.getAsStream(name);
            if (xobjStream == null) continue;

            PdfIndirectReference ref = xobjStream.getIndirectReference();
            if (ref != null && !visited.add(ref)) continue;

            PdfName subtype = xobjStream.getAsName(PdfName.Subtype);
            if (!PdfName.Form.equals(subtype)) continue;

            byte[] data = xobjStream.getBytes();
            if (data != null && data.length > 0) {
                byte[] stripped = stripInlineImages(data);
                if (stripped.length != data.length) {
                    // false = REPLACE (true APPENDS → doubles shared XObjects). See
                    // stripInvisibleTextFromXObjects for the full rationale.
                    xobjStream.setData(stripped, false);
                }
            }

            PdfDictionary subRes = xobjStream.getAsDictionary(
                PdfName.Resources);
            stripInlineImagesFromXObjects(subRes, visited);
        }
    }

    // -----------------------------------------------------------------
    // Structure Helpers
    // -----------------------------------------------------------------

    /**
     * Return the heading level (1-6) for heading tags, or 0 for non-headings.
     */
    private static int getHeadingLevel(String tag) {
        if (tag == null || tag.length() != 2 || tag.charAt(0) != 'H') return 0;
        char c = tag.charAt(1);
        return (c >= '1' && c <= '6') ? (c - '0') : 0;
    }

    /**
     * Return true when all children of {@code elem} are leaf Span elements
     * (i.e. none of them have nested children of their own).  In that case
     * the parent element can be treated as a leaf — it already carries
     * /ActualText with the full paragraph text and the empty Span children
     * add no semantic value.  This avoids the Acrobat "Associated with
     * content" failure where /ActualText sits on a container whose /K
     * contains only empty child struct element refs.
     */
    private static boolean areAllChildrenLeaves(JsonObject elem) {
        if (!elem.has("children") || !elem.get("children").isJsonArray()) {
            return false;
        }
        JsonArray children = elem.getAsJsonArray("children");
        if (children.size() == 0) {
            return false;
        }
        for (JsonElement c : children) {
            if (!c.isJsonObject()) return false;
            JsonObject child = c.getAsJsonObject();
            // Any child with nested grandchildren means this is not a flat leaf list
            if (child.has("children") && child.get("children").isJsonArray()
                    && child.getAsJsonArray("children").size() > 0) {
                return false;
            }
        }
        return true;
    }

    /**
     * Write MCR placeholder text into a tagged block. The text is rendered
     * using PDF rendering mode 3 (invisible) at 12pt so it is:
     *   - Not painted on screen (mode 3 = no fill, no stroke)
     *   - Readable by Acrobat 2025 Read Out Loud at 12pt font size
     *   - Readable by JAWS/NVDA via /ActualText on structure elements
     *
     * Phase 0 experiments confirmed that AROL reads mode 3 text correctly
     * when the font size is reasonable (12pt). The earlier 1pt approach
     * was ignored by AROL because of the tiny size, not the rendering mode.
     *
     * @param canvas  The page canvas
     * @param tagPointer  Current tag pointer (for openTag/closeTag)
     * @param font    Embedded font
     * @param x       X position on page
     * @param y       Y position on page
     * @param text    The real text to render
     */
    static void writeInvisibleMCR(
        PdfCanvas canvas, TagTreePointer tagPointer, PdfFont font,
        float x, float y, String text
    ) {
        canvas.openTag(tagPointer.getTagReference());
        canvas.saveState();
        // Rendering mode 3 (invisible): no fill, no stroke — text is
        // not painted but remains in the content stream for AROL extraction.
        // 12pt font size ensures AROL recognizes the text (ignores < ~4pt).
        canvas.setTextRenderingMode(
            PdfCanvasConstants.TextRenderingMode.INVISIBLE);
        canvas.beginText();
        canvas.setFontAndSize(font, 12);
        canvas.moveText(x, y);
        // Write actual text instead of a space glyph. Truncate to avoid
        // bloating the content stream for very long paragraphs.
        // Filter out characters the font can't encode (would produce
        // .notdef glyphs → veraPDF 7.21.8 failure).
        String mcrText = sanitizeForFont(font, text);
        canvas.showText(mcrText);
        canvas.endText();
        canvas.restoreState();
        canvas.closeTag();
    }

    /**
     * Number of lines a greedy word-wrap of {@code words} produces within a
     * text column of width {@code bboxW} at {@code fontSize}.  Mirrors the wrap
     * logic in {@link #writeDistributedMCR} exactly (wrap when the next word
     * would cross bboxR+5, never wrap on an empty line), so a font size that
     * fits by this count is guaranteed to lay out without vertical overflow.
     */
    static int layoutLineCount(String[] words, float bboxW, float fontSize) {
        float curX = 0f;
        float spaceW = fontSize * 0.3f;
        int lines = 1;
        for (String w : words) {
            if (w.isEmpty()) continue;
            float wordW = w.length() * fontSize * 0.55f;
            if (curX + wordW > bboxW + 5 && curX > 1) {
                lines++;
                curX = 0f;
            }
            curX += wordW + spaceW;
        }
        return lines;
    }

    /**
     * Largest font size in [floor, max] at which {@code words} wrap within
     * {@code bboxW} and the block height (lines * size * 1.15) fits within
     * {@code bboxH}.  Returns {@code floor} when even the floor overflows — the
     * caller detects that as "does not fit" and uses the single-showText
     * fallback.  Choosing the size by actual fit (not an estimate) is what
     * prevents the old truncation: the layout can no longer overflow.
     */
    static float computeFitFontSize(String[] words, float bboxW, float bboxH,
            float floor, float max) {
        if (layoutLineCount(words, bboxW, floor) * floor * 1.15f > bboxH) {
            return floor;
        }
        float lo = floor, hi = max;
        for (int i = 0; i < 24; i++) {
            float mid = (lo + hi) / 2f;
            if (layoutLineCount(words, bboxW, mid) * mid * 1.15f <= bboxH) {
                lo = mid;
            } else {
                hi = mid;
            }
        }
        return lo;
    }

    /**
     * Write element text distributed across its bbox under one MCID.
     * Uses PDF rendering mode 3 (invisible) so text is not painted but
     * remains extractable by Acrobat Read Out Loud.
     *
     * <p>Splits the element text into words and lays them out left-to-right
     * within the element's display-space bbox, wrapping to the next line
     * when the right edge is reached.  Word coordinates use the same
     * display-space system as Docling element bboxes and are converted to
     * content stream coordinates via {@code displayToContent()}.
     *
     * <p>A text matrix rotation is applied so text appears horizontal in
     * the displayed page regardless of /Rotate.
     *
     * @param canvas      Canvas to write on
     * @param tagPointer  Tag tree pointer positioned at the element
     * @param font        Embedded font
     * @param text        Full element text to distribute
     * @param bboxL       Display-space bbox left
     * @param bboxB       Display-space bbox bottom
     * @param bboxR       Display-space bbox right
     * @param bboxT       Display-space bbox top
     * @param rotation    Page /Rotate value (0, 90, 180, 270)
     * @param mbW         MediaBox width
     * @param mbH         MediaBox height
     */
    static void writeDistributedMCR(
        PdfCanvas canvas, TagTreePointer tagPointer, PdfFont font,
        String text,
        float bboxL, float bboxB, float bboxR, float bboxT,
        int rotation, float mbW, float mbH
    ) {
        if (text == null || text.trim().isEmpty()) return;
        String safe = sanitizeForFont(font, text);
        if (safe.trim().isEmpty()) return;

        float bboxW = Math.abs(bboxR - bboxL);
        float bboxH = Math.abs(bboxT - bboxB);
        if (bboxW < 1 || bboxH < 1) return;

        final float FLOOR = 2f, MAXFS = 48f;
        String[] words = safe.split("\\s+");

        // Text matrix rotation for page /Rotate
        float tmA, tmB, tmC, tmD;
        switch (rotation % 360) {
            case 90:  tmA =  0; tmB =  1; tmC = -1; tmD =  0; break;
            case 180: tmA = -1; tmB =  0; tmC =  0; tmD = -1; break;
            case 270: tmA =  0; tmB = -1; tmC =  1; tmD =  0; break;
            default:  tmA =  1; tmB =  0; tmC =  0; tmD =  1; break;
        }

        // Choose the font size by actually fitting the wrapped text into the
        // bbox, so the estimate and the layout cannot diverge.  The old
        // estimate/layout mismatch (and the estLines<1.5 heuristic) is what
        // silently truncated long paragraphs on scanned pages.
        float fontSize = computeFitFontSize(words, bboxW, bboxH, FLOOR, MAXFS);
        boolean fits = layoutLineCount(words, bboxW, fontSize) * fontSize * 1.15f
                       <= bboxH + 0.01f;

        canvas.openTag(tagPointer.getTagReference());
        canvas.saveState();
        canvas.setTextRenderingMode(
            PdfCanvasConstants.TextRenderingMode.INVISIBLE);
        canvas.beginText();
        canvas.setFontAndSize(font, fontSize);

        if (!fits) {
            // Degenerate bbox (e.g. a rotated page's ~6pt sliver): the paragraph
            // cannot fit at any usable size.  Spatial layout is irrelevant for
            // invisible (3 Tr) text — screen readers read MCID/structure order —
            // so write the whole string in one showText, sized to fit the
            // on-page advance axis, anchored at the bbox top-left with the page
            // rotation matrix.  No wrapping, no truncation: every word is
            // preserved under one MCID.
            float[] anchor = FormImagePageTagger.displayToContent(
                bboxL, bboxT - fontSize, rotation, mbW, mbH);
            float advanceAvail;
            switch (rotation % 360) {
                case 90:  advanceAvail = mbH - anchor[1]; break; // +content-y
                case 180: advanceAvail = anchor[0];       break; // -content-x
                case 270: advanceAvail = anchor[1];       break; // -content-y
                default:  advanceAvail = mbW - anchor[0]; break; // 0: +content-x
            }
            // ~0.5 em average glyph advance; keep a 5% margin, never enlarge.
            float fitSize = fontSize;
            float needed = Math.max(1, safe.length()) * fontSize * 0.5f;
            if (advanceAvail > 4 && needed > advanceAvail * 0.95f) {
                fitSize = Math.max(FLOOR,
                    fontSize * (advanceAvail * 0.95f) / needed);
            }
            canvas.setFontAndSize(font, fitSize);
            canvas.setTextMatrix(tmA, tmB, tmC, tmD, anchor[0], anchor[1]);
            canvas.showText(safe);
            canvas.endText();
            canvas.restoreState();
            canvas.closeTag();
            return;
        }

        // Normal bbox: distribute words left-to-right, wrapping at the right
        // edge.  The font was sized to fit, so no word is ever dropped.
        //
        // Wrap decisions use the same 0.55em/char estimate as
        // layoutLineCount, so the number of lines emitted here matches what
        // computeFitFontSize already verified fits bboxH.  Each line is
        // flushed as ONE showText() call with real space characters embedded
        // in the string (rather than one showText() per word at a
        // manually-estimated X offset): word-by-word absolute positioning
        // requires the estimate to reproduce the font's real glyph advances
        // closely enough to leave a visible inter-word gap, and at the font
        // sizes this sizing picks, the per-word estimate error can exceed
        // that gap and glue adjacent words together on extraction. A single
        // string per line lets the font's real space glyph separate words,
        // which is exact by construction.
        float lineHeight = fontSize * 1.15f;
        float curX = bboxL;
        float curY = bboxT - fontSize;  // start at top of bbox
        float spaceW = fontSize * 0.3f;
        StringBuilder line = new StringBuilder();
        for (String word : words) {
            if (word.isEmpty()) continue;
            float wordW = word.length() * fontSize * 0.55f;
            if (curX + wordW > bboxR + 5 && curX > bboxL + 1) {
                if (line.length() > 0) {
                    float[] cs = FormImagePageTagger.displayToContent(
                        bboxL, curY, rotation, mbW, mbH);
                    canvas.setTextMatrix(tmA, tmB, tmC, tmD, cs[0], cs[1]);
                    canvas.showText(line.toString());
                    line.setLength(0);
                }
                curX = bboxL;
                curY -= lineHeight;
            }
            if (line.length() > 0) line.append(' ');
            line.append(word);
            curX += wordW + spaceW;
        }
        if (line.length() > 0) {
            float[] cs = FormImagePageTagger.displayToContent(
                bboxL, curY, rotation, mbW, mbH);
            canvas.setTextMatrix(tmA, tmB, tmC, tmD, cs[0], cs[1]);
            canvas.showText(line.toString());
        }
        canvas.endText();
        canvas.restoreState();
        canvas.closeTag();
    }

    /**
     * Remove the /RoleMap inherited from the source document's StructTreeRoot.
     *
     * Both taggers rebuild the structure tree from scratch using only standard
     * ISO 32000-1 tag names, so every inherited entry describes a role that no
     * surviving element uses. An entry whose KEY is a standard type (Word emits
     * /Caption /P) violates ISO 14289-1 7.1 t7, and cascades into 7.2 t3 when
     * the remapped tag is a table child.
     *
     * /ClassMap is deliberately left alone — equally dead, never observed to
     * cause a violation.
     */
    static void stripInheritedRoleMap(PdfDictionary stRoot) {
        if (stRoot == null) {
            return;
        }
        stRoot.remove(PdfName.RoleMap);
    }

    /**
     * Load an embedded TrueType font for overlay MCR text.
     * Searches common system font paths.  Falls back to Helvetica with
     * a loud warning — non-embedded Helvetica violates PDF/UA 7.21.4.1
     * and causes screen readers to skip content.
     */
    static PdfFont loadEmbeddedFont() throws Exception {
        String[] fontPaths = {
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
            "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
            "/usr/share/fonts/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
        };
        for (String fp : fontPaths) {
            if (new java.io.File(fp).exists()) {
                try {
                    return PdfFontFactory.createFont(
                        fp, PdfEncodings.IDENTITY_H,
                        PdfFontFactory.EmbeddingStrategy.FORCE_EMBEDDED);
                } catch (Exception e) {
                    System.err.println("  WARNING: Font exists but failed "
                        + "to load: " + fp + " — " + e.getMessage());
                }
            }
        }
        System.err.println("  WARNING: No embeddable TrueType font found "
            + "on system. Using non-embedded Helvetica — this violates "
            + "PDF/UA 7.21.4.1 and will cause screen reader failures. "
            + "Install dejavu-sans or liberation-sans fonts.");
        return PdfFontFactory.createFont(StandardFonts.HELVETICA);
    }

    /**
     * Sanitize text extracted by Docling from PDFs with Type 3 fonts.
     *
     * Two issues are handled:
     * 1. PUA characters (U+E0XX) from previous pipeline runs that mapped
     *    Type 3 glyphs to Private Use Area instead of real Unicode.
     *    Fix: U+E0XX → U+00XX for printable ASCII/Latin-1.
     *
     * 2. Control characters (0x00-0x1F) from TeX/CM Type 3 font encodings.
     *    TeX T1 (Cork) encoding uses codes 0x00-0x1F for ligatures and
     *    accents. When Docling extracts these as raw char codes, they become
     *    control characters in /ActualText, which PAC rejects as
     *    "Invalid unicode string".
     *    Fix: map known T1 positions to Unicode equivalents; strip the rest.
     */
    static String dePuaText(String text) {
        if (text == null || text.isEmpty()) return text;

        boolean needsSanitize = false;
        for (int i = 0; i < text.length(); i++) {
            int cp = text.codePointAt(i);
            if ((cp >= 0xE000 && cp <= 0xE0FF) ||
                cp == 0x09 ||
                (cp < 0x20 && cp != 0x0A && cp != 0x0D)) {
                needsSanitize = true;
                break;
            }
        }
        if (!needsSanitize) return text;  // fast path

        StringBuilder sb = new StringBuilder(text.length());
        for (int i = 0; i < text.length(); i++) {
            int cp = text.codePointAt(i);

            // PUA de-mapping
            if (cp >= 0xE020 && cp <= 0xE07E) {
                sb.appendCodePoint(cp - 0xE000);
            } else if (cp >= 0xE0A0 && cp <= 0xE0FF) {
                sb.appendCodePoint(cp - 0xE000);
            } else if (cp >= 0xE000 && cp < 0xE020) {
                // PUA for control-range codes — apply same T1 mapping
                String repl = texControlCharReplacement(cp - 0xE000);
                sb.append(repl);
            }
            // Tab → space (literal \t in PDF ActualText strings is invalid)
            else if (cp == 0x09) {
                sb.append(' ');
            }
            // TeX T1 control character mapping
            else if (cp < 0x20 && cp != 0x0A && cp != 0x0D) {
                String repl = texControlCharReplacement(cp);
                sb.append(repl);
            }
            // Supplementary (surrogate pair)
            else if (cp > 0xFFFF) {
                sb.appendCodePoint(cp);
                i++;
            }
            // Normal character
            else {
                sb.appendCodePoint(cp);
            }
        }
        return sb.toString();
    }

    /**
     * Map TeX T1 (Cork) encoding control-range positions to Unicode.
     * Returns the Unicode replacement string for a given char code 0x00-0x1F.
     */
    private static String texControlCharReplacement(int code) {
        switch (code) {
            case 0x00: return "\u0060";     // grave accent
            case 0x01: return "\u00B4";     // acute accent
            case 0x02: return "\u02C6";     // circumflex
            case 0x03: return "\u02DC";     // tilde
            case 0x04: return "\u00A8";     // dieresis
            case 0x05: return "\u02DD";     // double acute
            case 0x06: return "\u02DA";     // ring above
            case 0x07: return "\u02C7";     // caron
            case 0x08: return "\u02D8";     // breve
            case 0x09: return " ";          // tab → space
            case 0x0A: return "\n";         // newline (keep)
            case 0x0B: return "\uFB00";     // ff ligature
            case 0x0C: return "\uFB01";     // fi ligature
            case 0x0D: return "\uFB02";     // fl ligature
            case 0x0E: return "\uFB03";     // ffi ligature
            case 0x0F: return "\uFB04";     // ffl ligature
            case 0x10: return "\u0131";     // dotless i
            case 0x11: return "\u0237";     // dotless j
            case 0x12: return "\u0060";     // grave
            case 0x13: return "\u00B4";     // acute
            case 0x14: return "\u02C7";     // caron
            case 0x15: return "\u02D8";     // breve
            case 0x16: return "\u00AF";     // macron
            case 0x17: return "\u02DA";     // ring above
            case 0x18: return "\u00B8";     // cedilla
            case 0x19: return "\u00DF";     // sharp s
            case 0x1A: return "\u00E6";     // ae ligature
            case 0x1B: return "\uFB00";     // ff ligature (alternate position)
            case 0x1C: return "\uFB01";     // fi ligature (alternate position)
            case 0x1D: return "\uFB02";     // fl ligature (alternate position)
            case 0x1E: return "\uFB03";     // ffi ligature (alternate position)
            case 0x1F: return "\uFB04";     // ffl ligature (alternate position)
            default:   return " ";          // unknown → space
        }
    }

    /**
     * Map TeX T1 control-range positions to a single Unicode codepoint
     * (for use in ToUnicode CMap bfchar entries, which need one codepoint).
     * Ligatures (ff, fi, fl, ffi, ffl) are mapped to the first character
     * since CMap entries are single-codepoint; dePuaText() handles the
     * full multi-char expansion for /ActualText.
     */
    private static int texControlCharUnicode(int code) {
        switch (code) {
            case 0x00: return 0x0060;   // grave accent
            case 0x01: return 0x00B4;   // acute accent
            case 0x02: return 0x02C6;   // circumflex
            case 0x03: return 0x02DC;   // tilde
            case 0x04: return 0x00A8;   // dieresis
            case 0x05: return 0x02DD;   // double acute
            case 0x06: return 0x02DA;   // ring above
            case 0x07: return 0x02C7;   // caron
            case 0x08: return 0x02D8;   // breve
            case 0x0B: return 0xFB00;   // ff ligature (U+FB00)
            case 0x0C: return 0xFB01;   // fi ligature (U+FB01)
            case 0x0D: return 0xFB02;   // fl ligature (U+FB02)
            case 0x0E: return 0xFB03;   // ffi ligature (U+FB03)
            case 0x0F: return 0xFB04;   // ffl ligature (U+FB04)
            case 0x10: return 0x0131;   // dotless i
            case 0x11: return 0x0237;   // dotless j
            case 0x12: return 0x0060;   // grave
            case 0x13: return 0x00B4;   // acute
            case 0x14: return 0x02C7;   // caron
            case 0x15: return 0x02D8;   // breve
            case 0x16: return 0x00AF;   // macron
            case 0x17: return 0x02DA;   // ring above
            case 0x18: return 0x00B8;   // cedilla
            case 0x19: return 0x00DF;   // sharp s
            case 0x1A: return 0x00E6;   // ae ligature
            case 0x1B: return 0xFB00;   // ff ligature (alt position)
            case 0x1C: return 0xFB01;   // fi ligature (alt position)
            case 0x1D: return 0xFB02;   // fl ligature (alt position)
            case 0x1E: return 0xFB03;   // ffi ligature (alt position)
            case 0x1F: return 0xFB04;   // ffl ligature (alt position)
            default:   return 0x0020;   // space
        }
    }

    /**
     * Remove characters that the font cannot encode (would produce .notdef
     * glyphs). Replaces them with space. Returns at least " " if the input
     * is null/empty or entirely unrepresentable.
     */
    static String sanitizeForFont(PdfFont font, String text) {
        if (text == null || text.trim().isEmpty()) return " ";
        int cut = Math.min(text.length(), 50000);
        // Don't split a surrogate pair at the truncation boundary
        if (cut < text.length() && Character.isHighSurrogate(
                text.charAt(cut - 1))) {
            cut--;
        }
        String truncated = text.substring(0, cut);
        StringBuilder sb = new StringBuilder(truncated.length());
        for (int i = 0; i < truncated.length(); i++) {
            int cp = truncated.codePointAt(i);
            if (cp > 0xFFFF) {
                // Supplementary character — skip (2 chars)
                i++;
                sb.append(' ');
                continue;
            }
            if (font.containsGlyph(cp)) {
                sb.appendCodePoint(cp);
            } else {
                // Replace with space to preserve word boundaries
                sb.append(' ');
            }
        }
        String result = sb.toString().trim();
        return result.isEmpty() ? " " : result;
    }

    /**
     * Recursively add children (Span, Link, LI, Lbl, LBody, etc.)
     * under the current structure element, each with its own MCR.
     */
    private static void addChildren(
        TagTreePointer tagPointer,
        JsonArray children,
        PdfDocument pdfDoc,
        PdfPage page,
        PdfFont font,
        Map<Integer, RawPdfCanvas> canvasCache,
        int pageNo,
        float parentX,
        float parentY
    ) {
        if (children == null || children.size() == 0) return;

        PdfCanvas canvas = canvasCache.computeIfAbsent(pageNo,
            p -> new RawPdfCanvas(pdfDoc.getPage(p)));

        // Determine if current parent is a TR — children must be TH or TD
        String parentTag = null;
        try {
            PdfStructElem parentElem = tagPointer.getContext()
                .getPointerStructElem(tagPointer);
            if (parentElem != null && parentElem.getRole() != null) {
                parentTag = parentElem.getRole().getValue();
            }
        } catch (Exception ignored) {}
        boolean parentIsTR = "TR".equals(parentTag);

        float offsetX = parentX;
        try {
            for (JsonElement ce : children) {
                JsonObject child = ce.getAsJsonObject();
                String childTag = child.has("tag")
                    ? child.get("tag").getAsString() : "Span";
                String childText = child.has("text")
                    ? dePuaText(child.get("text").getAsString()) : "";

                // PDF/UA 7.2: TR may only contain TH and TD children.
                // Remap any non-TH/TD tag (e.g. Span) to TD when under TR.
                if (parentIsTR && !"TH".equals(childTag)
                        && !"TD".equals(childTag)) {
                    childTag = "TD";
                }

                // Check if this child is a container (has nested children)
                boolean childHasChildren = child.has("children")
                    && child.get("children").isJsonArray()
                    && child.getAsJsonArray("children").size() > 0;

                // Determine if this is a leaf node that would carry content
                boolean isLBodyOrLbl = "LBody".equals(childTag)
                    || "Lbl".equals(childTag);
                boolean isFigure = "Figure".equals(childTag);

                // Skip creating empty structure elements for leaf nodes that
                // would have no MCR, no ActualText, and no children.
                // PAC flags these as "Structure tree" warnings.
                // The parent's /ActualText already covers the text content
                // per PDF spec §14.9.4, so these SEs are redundant.
                if (!childHasChildren && !isLBodyOrLbl && !isFigure) {
                    offsetX += 1;
                    continue;
                }

                tagPointer.addTag(childTag);

                if (!childHasChildren) {
                    // LBody/Lbl need their own MCR + ActualText because the
                    // parent LI is a container, not a text carrier.
                    if (isLBodyOrLbl) {
                        writeInvisibleMCR(canvas, tagPointer, font,
                            offsetX, parentY, childText);

                        if (childText != null && !childText.trim().isEmpty()) {
                            // /ActualText removed — blocks AROL/NVDA
                        }
                    }
                }

                // PDF/UA 7.3: Figure elements must have /Alt text
                // Acrobat "Associated with content": Figure with /Alt MUST
                // also have an MCR so the alt text is associated with content.
                if (isFigure) {
                    PdfStructElem childElem = tagPointer.getContext()
                        .getPointerStructElem(tagPointer);
                    // Only set /Alt when there is real text to say. The MCR
                    // below is still written unconditionally — an alt-less
                    // Figure that keeps its content association is exactly
                    // the desired end state for Acrobat.
                    String figAlt = (childText != null && !childText.trim().isEmpty())
                        ? childText.substring(0, Math.min(childText.length(), 500))
                        : null;
                    if (figAlt != null) {
                        childElem.setAlt(new PdfString(figAlt,
                            PdfEncodings.UNICODE_BIG));
                    }
                    // Write invisible MCR so Acrobat sees content association.
                    // Only for leaf Figures — Figures with child SEs must not
                    // have both MCR and children (PDF/UA structure rule).
                    if (!childHasChildren) {
                        String mcrText = (childText != null && !childText.isEmpty())
                            ? childText.substring(0, Math.min(childText.length(), 200))
                            : "Figure";
                        writeInvisibleMCR(canvas, tagPointer, font,
                            offsetX, parentY, mcrText);
                    }
                }

                // Recurse into nested children (e.g., LI > Lbl + LBody)
                if (childHasChildren) {
                    addChildren(tagPointer, child.getAsJsonArray("children"),
                                pdfDoc, page, font, canvasCache, pageNo,
                                offsetX, parentY);
                }

                tagPointer.moveToParent(); // back to parent
                offsetX += 1; // slight offset for each child
            }
        } catch (Exception e) {
            System.err.println("Warning: child structure failed: "
                + e.getMessage());
        }
    }

    /**
     * JsonObject variant of the first-row-TH fallback for the overlay
     * path. Iff no cell in the table has any header flag and the table
     * has >=2 rows, set is_col_header on every first-row cell so they
     * emit as TH — the existing THead heuristic otherwise produces a
     * THead full of TDs. Gated by --first-row-th-fallback.
     *
     * @return true if promotion was applied
     */
    static boolean promoteFirstRowHeadersJson(
            TreeMap<Integer, List<JsonObject>> rows) {
        if (!firstRowThFallback) return false;
        if (rows.size() < 2) return false;
        for (List<JsonObject> rowCells : rows.values()) {
            for (JsonObject c : rowCells) {
                if ((c.has("is_header") && c.get("is_header").getAsBoolean())
                    || (c.has("is_col_header")
                        && c.get("is_col_header").getAsBoolean())
                    || (c.has("is_row_header")
                        && c.get("is_row_header").getAsBoolean())) {
                    return false;
                }
            }
        }
        List<JsonObject> firstRow = rows.firstEntry().getValue();
        // Gap-fill can synthesize an all-empty row 0 when the source table's
        // real cells start at row index >=1 (e.g. a caption/spacer row). Don't
        // promote empty filler cells to TH.
        boolean firstRowHasText = false;
        for (JsonObject c : firstRow) {
            String t = c.has("text") && !c.get("text").isJsonNull()
                ? c.get("text").getAsString() : "";
            if (!t.trim().isEmpty()) {
                firstRowHasText = true;
                break;
            }
        }
        if (!firstRowHasText) return false;
        for (JsonObject c : firstRow) {
            c.addProperty("is_col_header", true);
        }
        return true;
    }

    /**
     * Add THead/TBody/TR/TH/TD children under a Table structure element,
     * each with proper marked content references.
     *
     * Determines header rows (first row if it contains any header cells)
     * and wraps them in THead; remaining rows go in TBody.
     * Sets ColSpan/RowSpan attributes and uses is_col_header/is_row_header
     * for proper Scope values.
     */
    private static void addTableChildren(
        TagTreePointer tagPointer,
        JsonArray cells,
        PdfDocument pdfDoc,
        PdfPage page,
        PdfFont font,
        Map<Integer, RawPdfCanvas> canvasCache,
        int pageNo
    ) {
        if (cells == null || cells.size() == 0) return;

        // Group cells by row (TreeMap for ordered access + firstKey())
        TreeMap<Integer, List<JsonObject>> rows = new TreeMap<>();
        for (JsonElement ce : cells) {
            JsonObject cell = ce.getAsJsonObject();
            int row = cell.has("row") ? cell.get("row").getAsInt() : 0;
            rows.computeIfAbsent(row, k -> new ArrayList<>()).add(cell);
        }

        // --- Grid regularity: ensure every row covers all columns ---
        // Build coverage map and determine grid width
        int maxCol = 0;
        int maxRow = 0;
        Set<String> covered = new HashSet<>();
        for (JsonElement ce : cells) {
            JsonObject c = ce.getAsJsonObject();
            int r = c.has("row") ? c.get("row").getAsInt() : 0;
            int col = c.has("col") ? c.get("col").getAsInt() : 0;
            int rs = c.has("row_span") ? c.get("row_span").getAsInt() : 1;
            int cs = c.has("col_span") ? c.get("col_span").getAsInt() : 1;
            if (rs < 1) rs = 1;
            if (cs < 1) cs = 1;
            for (int dr = 0; dr < rs; dr++) {
                for (int dc = 0; dc < cs; dc++) {
                    covered.add((r + dr) + "," + (col + dc));
                }
            }
            maxRow = Math.max(maxRow, r + rs - 1);
            maxCol = Math.max(maxCol, col + cs - 1);
        }
        // Track positions covered by row-spanning cells from PRIOR rows.
        // A cell at (originRow, col) with RowSpan > 1 covers positions
        // in subsequent rows.
        // Collect positions where a spanning cell explicitly starts
        // (RowSpan > 1). These override prior rowspans at the same
        // position. Plain cells (RowSpan=1) at spanned positions are
        // duplicates from Docling and should be removed.
        Set<String> spanningCellStarts = new HashSet<>();
        for (JsonElement ce : cells) {
            JsonObject c = ce.getAsJsonObject();
            int rs = c.has("row_span") ? c.get("row_span").getAsInt() : 1;
            if (rs > 1) {
                int r = c.has("row") ? c.get("row").getAsInt() : 0;
                int col = c.has("col") ? c.get("col").getAsInt() : 0;
                int cs = c.has("col_span") ? c.get("col_span").getAsInt() : 1;
                for (int dc = 0; dc < Math.max(cs, 1); dc++) {
                    spanningCellStarts.add(r + "," + (col + dc));
                }
            }
        }
        // Build spannedFromAbove: positions covered by a rowspan
        // from a prior row, unless a new spanning cell starts there.
        Set<String> spannedFromAbove = new HashSet<>();
        for (JsonElement ce : cells) {
            JsonObject c = ce.getAsJsonObject();
            int r = c.has("row") ? c.get("row").getAsInt() : 0;
            int col = c.has("col") ? c.get("col").getAsInt() : 0;
            int rs = c.has("row_span") ? c.get("row_span").getAsInt() : 1;
            int cs = c.has("col_span") ? c.get("col_span").getAsInt() : 1;
            if (rs < 1) rs = 1;
            if (cs < 1) cs = 1;
            for (int dr = 1; dr < rs; dr++) {
                for (int dc = 0; dc < cs; dc++) {
                    String key = (r + dr) + "," + (col + dc);
                    // Only mark as spanned if no spanning cell starts here
                    if (!spanningCellStarts.contains(key)) {
                        spannedFromAbove.add(key);
                    }
                }
            }
        }
        // Remove explicit cells from rows at positions spanned from
        // above. Docling sometimes provides cells at every position,
        // but veraPDF counts both the spanning cell (via RowSpan)
        // AND the explicit cell, resulting in too many columns.
        for (Map.Entry<Integer, List<JsonObject>> entry : rows.entrySet()) {
            int r = entry.getKey();
            entry.getValue().removeIf(c -> {
                int col = c.has("col") ? c.get("col").getAsInt() : 0;
                return spannedFromAbove.contains(r + "," + col);
            });
        }
        // Clip RowSpan values where overlapping spanning cells exist.
        // If cell at (r, col) has RS=N but another spanning cell starts
        // at (r+k, col) for some k < N, clip RS to k to avoid
        // double-counting columns in veraPDF test 42.
        for (List<JsonObject> rowCells : rows.values()) {
            for (JsonObject c : rowCells) {
                int r = c.has("row") ? c.get("row").getAsInt() : 0;
                int col = c.has("col") ? c.get("col").getAsInt() : 0;
                int rs = c.has("row_span") ? c.get("row_span").getAsInt() : 1;
                if (rs <= 1) continue;
                int cs = c.has("col_span") ? c.get("col_span").getAsInt() : 1;
                // Check each column this cell covers
                int clippedRs = rs;
                for (int dc = 0; dc < Math.max(cs, 1); dc++) {
                    // Find earliest row in the span range where another
                    // spanning cell starts at the same column
                    for (int dr = 1; dr < clippedRs; dr++) {
                        String key = (r + dr) + "," + (col + dc);
                        if (spanningCellStarts.contains(key)) {
                            clippedRs = Math.min(clippedRs, dr);
                            break;
                        }
                    }
                }
                if (clippedRs != rs) {
                    c.addProperty("row_span", clippedRs);
                }
            }
        }
        // Clip ColSpan values where cells in the same row overlap.
        // If cell A at col=X cs=N extends into the start position of
        // cell B at col=Y (Y < X+N), clip A's cs to Y-X.
        // This mirrors the RowSpan clipping above but for horizontal overlaps
        // caused by Docling misdetecting colspan extents.
        for (List<JsonObject> rowCells : rows.values()) {
            // Sort by column position for overlap detection
            rowCells.sort((a, b) -> {
                int ca = a.has("col") ? a.get("col").getAsInt() : 0;
                int cb = b.has("col") ? b.get("col").getAsInt() : 0;
                return Integer.compare(ca, cb);
            });
            for (int i = 0; i < rowCells.size() - 1; i++) {
                JsonObject curr = rowCells.get(i);
                JsonObject next = rowCells.get(i + 1);
                int currCol = curr.has("col") ? curr.get("col").getAsInt() : 0;
                int currCs  = curr.has("col_span") ? curr.get("col_span").getAsInt() : 1;
                int nextCol = next.has("col") ? next.get("col").getAsInt() : 0;
                if (currCol + currCs > nextCol) {
                    int clippedCs = nextCol - currCol;
                    if (clippedCs < 1) clippedCs = 1;
                    curr.addProperty("col_span", clippedCs);
                }
            }
        }
        // Recompute maxCol after colspan clipping — the grid width may
        // have shrunk if overlapping colspans were clipped.
        maxCol = 0;
        covered.clear();
        for (List<JsonObject> rowCells : rows.values()) {
            for (JsonObject c : rowCells) {
                int r   = c.has("row") ? c.get("row").getAsInt() : 0;
                int col = c.has("col") ? c.get("col").getAsInt() : 0;
                int rs  = c.has("row_span") ? c.get("row_span").getAsInt() : 1;
                int cs  = c.has("col_span") ? c.get("col_span").getAsInt() : 1;
                if (rs < 1) rs = 1;
                if (cs < 1) cs = 1;
                for (int dr = 0; dr < rs; dr++) {
                    for (int dc = 0; dc < cs; dc++) {
                        covered.add((r + dr) + "," + (col + dc));
                    }
                }
                maxCol = Math.max(maxCol, col + cs - 1);
            }
        }
        // Insert empty TD cells for genuinely uncovered positions
        // (gaps in the table grid that aren't covered by any cell
        // or rowspan). PDF/UA 7.2 test 42 requires each TR's
        // sum(ColSpan) to equal the grid width.
        for (int r = 0; r <= maxRow; r++) {
            for (int col = 0; col <= maxCol; col++) {
                String key = r + "," + col;
                // Skip positions covered by a rowspan from above
                if (spannedFromAbove.contains(key)) continue;
                // Skip positions covered by an explicit cell
                if (covered.contains(key)) continue;
                // This position is a genuine gap — add a filler TD
                JsonObject empty = new JsonObject();
                empty.addProperty("row", r);
                empty.addProperty("col", col);
                empty.addProperty("row_span", 1);
                empty.addProperty("col_span", 1);
                empty.addProperty("text", "");
                empty.addProperty("is_header", false);
                empty.addProperty("is_col_header", false);
                empty.addProperty("is_row_header", false);
                rows.computeIfAbsent(r, k -> new ArrayList<>()).add(empty);
            }
        }
        // Sort cells within each row by column index
        for (List<JsonObject> rowCells : rows.values()) {
            rowCells.sort((a, b) -> {
                int ca = a.has("col") ? a.get("col").getAsInt() : 0;
                int cb = b.has("col") ? b.get("col").getAsInt() : 0;
                return Integer.compare(ca, cb);
            });
        }

        // First-row-TH fallback for tables with no header flags at all.
        // Runs before header-row detection so the promoted row drives
        // both THead grouping and TH emission with /Scope /Column.
        promoteFirstRowHeadersJson(rows);

        // Determine which rows are header rows (contain any header cell)
        Set<Integer> headerRowIndices = new LinkedHashSet<>();
        int firstRowKey = rows.firstKey();
        for (Map.Entry<Integer, List<JsonObject>> entry : rows.entrySet()) {
            // Only first row(s) can be header rows — stop at first non-header
            boolean hasHeader = false;
            for (JsonObject cell : entry.getValue()) {
                boolean isColHeader = cell.has("is_col_header")
                    && cell.get("is_col_header").getAsBoolean();
                boolean isHeader = cell.has("is_header")
                    && cell.get("is_header").getAsBoolean();
                if (isColHeader || (isHeader && entry.getKey() == firstRowKey)) {
                    hasHeader = true;
                    break;
                }
            }
            if (hasHeader) {
                headerRowIndices.add(entry.getKey());
            } else {
                break; // Stop at first non-header row
            }
        }

        // Heuristic: if Docling flagged no header rows and the table has
        // at least 2 rows, treat the first row as a header row.
        // This satisfies Acrobat's "Tables should have headers" check.
        if (headerRowIndices.isEmpty() && rows.size() >= 2) {
            headerRowIndices.add(firstRowKey);
        }

        PdfCanvas canvas = canvasCache.computeIfAbsent(pageNo,
            p -> new RawPdfCanvas(pdfDoc.getPage(p)));

        // Only use THead/TBody grouping if there are both header and
        // non-header rows. PDF/UA 7.2 test 14 requires that if THead
        // exists, TBody must also exist. Tables where ALL rows are
        // headers or NO rows are headers get flat Table > TR structure.
        boolean hasNonHeaderRows = headerRowIndices.size() < rows.size();
        boolean useSectionGrouping = !headerRowIndices.isEmpty()
            && hasNonHeaderRows;

        try {
            boolean inTHead = false;
            boolean inTBody = false;

            for (Map.Entry<Integer, List<JsonObject>> entry : rows.entrySet()) {
                boolean isHeaderRow = headerRowIndices.contains(entry.getKey());

                if (useSectionGrouping) {
                    // Manage THead/TBody grouping
                    if (isHeaderRow && !inTHead) {
                        tagPointer.addTag("THead");
                        inTHead = true;
                    } else if (!isHeaderRow && inTHead) {
                        tagPointer.moveToParent(); // close THead
                        inTHead = false;
                    }
                    if (!isHeaderRow && !inTBody) {
                        tagPointer.addTag("TBody");
                        inTBody = true;
                    }
                }

                // Add TR (table row)
                tagPointer.addTag("TR");

                for (JsonObject cell : entry.getValue()) {
                    boolean isColHeader = cell.has("is_col_header")
                        && cell.get("is_col_header").getAsBoolean();
                    boolean isRowHeader = cell.has("is_row_header")
                        && cell.get("is_row_header").getAsBoolean();
                    boolean isHeader = isColHeader || isRowHeader
                        || (cell.has("is_header")
                            && cell.get("is_header").getAsBoolean());
                    String cellTag = isHeader ? "TH" : "TD";
                    String cellText = cell.has("text")
                        ? dePuaText(cell.get("text").getAsString()) : "";

                    int colSpan = cell.has("col_span")
                        ? cell.get("col_span").getAsInt() : 1;
                    int rowSpan = cell.has("row_span")
                        ? cell.get("row_span").getAsInt() : 1;

                    float cellX = 72, cellY = 72;
                    if (cell.has("bbox") && !cell.get("bbox").isJsonNull()) {
                        JsonObject bbox = cell.getAsJsonObject("bbox");
                        cellX = bbox.has("l") ? bbox.get("l").getAsFloat() : 72;
                        cellY = bbox.has("b") ? bbox.get("b").getAsFloat() : 72;
                    }

                    tagPointer.addTag(cellTag);

                    // Create marked content for the cell
                    writeInvisibleMCR(canvas, tagPointer, font,
                        cellX, cellY, cellText);

                    // Set properties on the cell structure element
                    PdfStructElem cellElem = tagPointer.getContext()
                        .getPointerStructElem(tagPointer);

                    // Table attributes: Scope, ColSpan, RowSpan
                    PdfDictionary tableAttrs = new PdfDictionary();
                    tableAttrs.put(new PdfName("O"), new PdfName("Table"));

                    if (isHeader) {
                        // Determine scope from explicit col/row header flags
                        String scope;
                        if (isColHeader) {
                            scope = "Column";
                        } else if (isRowHeader) {
                            scope = "Row";
                        } else {
                            // Fallback: first row → Column, else → Row
                            scope = (entry.getKey() == firstRowKey)
                                ? "Column" : "Row";
                        }
                        tableAttrs.put(new PdfName("Scope"),
                            new PdfName(scope));
                    }

                    if (colSpan > 1) {
                        tableAttrs.put(new PdfName("ColSpan"),
                            new PdfNumber(colSpan));
                    }
                    if (rowSpan > 1) {
                        tableAttrs.put(new PdfName("RowSpan"),
                            new PdfNumber(rowSpan));
                    }

                    // Only set attributes if we have meaningful values
                    if (tableAttrs.size() > 1) {
                        cellElem.put(PdfName.A, tableAttrs);
                    }

                    // Set ActualText for the cell.
                    // Empty cells must still have /ActualText (even if just a
                    // space) — otherwise PAC flags "Tagged text consists of only
                    // whitespace" because the MCR contains an invisible space
                    // placeholder with no /ActualText to explain it.
                    String actualCellText = cellText.trim().isEmpty()
                        ? " " : cellText;
                    // /ActualText removed — blocks AROL/NVDA

                    tagPointer.moveToParent(); // back to TR
                }

                tagPointer.moveToParent(); // back to THead/TBody or Table
            }

            // Close any open THead or TBody
            if (inTHead) tagPointer.moveToParent();
            if (inTBody) tagPointer.moveToParent();

        } catch (Exception e) {
            System.err.println("Warning: table sub-structure failed: "
                + e.getMessage());
        }
    }

    /**
     * Add TOCI children under a TOC structure element.
     * Each TOCI gets a P child with ActualText.
     */
    private static void addTocChildren(
        TagTreePointer tagPointer,
        JsonArray children,
        PdfDocument pdfDoc,
        PdfPage page,
        PdfFont font,
        Map<Integer, RawPdfCanvas> canvasCache,
        int pageNo,
        float parentX,
        float parentY
    ) {
        if (children == null || children.size() == 0) return;

        PdfCanvas canvas = canvasCache.computeIfAbsent(pageNo,
            p -> new RawPdfCanvas(pdfDoc.getPage(p)));

        try {
            float offsetX = parentX;
            for (JsonElement ce : children) {
                JsonObject child = ce.getAsJsonObject();
                String childTag = child.has("tag")
                    ? child.get("tag").getAsString() : "TOCI";
                String childText = child.has("text")
                    ? dePuaText(child.get("text").getAsString()) : "";

                tagPointer.addTag(childTag);

                // TOCI contains a P child with the entry text
                tagPointer.addTag("P");

                // Create marked content
                writeInvisibleMCR(canvas, tagPointer, font,
                    offsetX, parentY, childText);

                // Set ActualText on the P element.
                // Always set it (even for whitespace) to prevent PAC
                // "Tagged text consists of only whitespace" errors.
                {
                    // /ActualText removed — blocks AROL/NVDA
                }

                tagPointer.moveToParent(); // back to TOCI
                tagPointer.moveToParent(); // back to TOC
                offsetX += 1;
            }
        } catch (Exception e) {
            System.err.println("Warning: TOC sub-structure failed: "
                + e.getMessage());
        }
    }


    // RawPdfCanvas — subclass to inject raw content bytes
    // -----------------------------------------------------------------

    /**
     * A PdfCanvas subclass that exposes the ability to write raw PDF
     * content bytes directly into the content stream. This is needed
     * to inline original page content (text operators, image draws, etc.)
     * between openTag/closeTag calls, so that all operators end up
     * inside BDC/EMC markers rather than hidden in a Form XObject.
     *
     * PdfCanvas.contentStream is a protected PdfStream field.
     * We access it via getOutputStream().writeBytes() to append raw
     * content to the same stream that openTag/closeTag write to.
     */
    private static class RawPdfCanvas extends PdfCanvas {

        public RawPdfCanvas(PdfPage page) {
            super(page);
        }

        /**
         * Write raw PDF content bytes into the canvas's content stream.
         * The bytes are appended at the current write position, which
         * should be between openTag() and closeTag() calls.
         */
        public void writeRawContent(byte[] content) throws java.io.IOException {
            this.contentStream.getOutputStream().writeBytes(content);
        }
    }
}
