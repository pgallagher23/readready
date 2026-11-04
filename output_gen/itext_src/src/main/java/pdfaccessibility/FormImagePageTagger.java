// SPDX-License-Identifier: AGPL-3.0-or-later
/*
 * FormImagePageTagger — handles tagging for image-based fillable form pages.
 *
 * Image-based forms have a rasterized background image (not text in the
 * content stream) with Widget annotation overlays.  The content-preserving
 * tagger can't find text runs to match, so normally these pages get the
 * overlay fallback: Artifact-wrap + invisible 1pt white MCR text.
 *
 * Acrobat Read Out Loud ignores that invisible text and reports the page
 * as empty.  This class fixes the problem by:
 *   1. Keeping the form background VISIBLE — tagged as Figure with /Alt
 *      (instead of Artifact-wrapped)
 *   2. Writing MCRs for Docling-extracted elements at correct positions,
 *      accounting for page rotation (/Rotate) and using per-child bboxes
 *   3. tagWidgetAnnotations() in ContentPreservingTagger handles Form SEs
 *      with OBJR separately (no changes needed there)
 */
package pdfaccessibility;

import com.itextpdf.io.font.PdfEncodings;
import com.itextpdf.kernel.font.PdfFont;
import com.itextpdf.kernel.pdf.*;
import com.itextpdf.kernel.pdf.canvas.PdfCanvas;
import com.itextpdf.kernel.pdf.tagging.PdfStructElem;
import com.itextpdf.kernel.pdf.tagutils.TagTreePointer;
import com.itextpdf.kernel.pdf.tagutils.TagReference;

import java.util.List;
import java.util.Map;
import java.util.Set;

public class FormImagePageTagger {

    /**
     * Transform displayed coordinates (Docling bbox space) to content
     * stream coordinates (MediaBox space) for rotated pages.
     *
     * Docling reports bboxes in post-rotation visual space, but
     * PdfCanvas.moveText() uses pre-rotation MediaBox coordinates.
     * This applies the inverse of the page's /Rotate transform.
     *
     * Verified with round-trip tests for all rotation values.
     * Matches the inverse of content_matcher._transform_run_coords().
     */
    static float[] displayToContent(float dx, float dy,
            int rotation, float mbWidth, float mbHeight) {
        switch (rotation % 360) {
            case 90:  return new float[]{mbWidth - dy, dx};
            case 180: return new float[]{mbWidth - dx, mbHeight - dy};
            case 270: return new float[]{dy, mbHeight - dx};
            default:  return new float[]{dx, dy};
        }
    }

    /**
     * Tag a form-image page: keep the form background visible as Figure,
     * write MCRs for Docling-extracted elements at correct positions.
     *
     * @return number of elements tagged with overlay MCRs
     */
    public static int tagFormImagePage(
        PdfDocument pdfDoc,
        PdfPage page,
        int pageNum,
        TagTreePointer tagPointer,
        List<ContentPreservingTagger.ElementInfo> allElements,
        Map<String, int[]> elementTreePath,
        PdfFont overlayFont,
        Set<String> processedElems
    ) throws Exception {

        // 1. Tag page content as Figure (keeps form background visible)
        tagPageContentAsFigure(pdfDoc, page, tagPointer,
            "Fillable form background");

        // 2. Ensure StructParents for MCR registration
        if (page.getPdfObject().get(new PdfName("StructParents")) == null) {
            page.getPdfObject().put(new PdfName("StructParents"),
                new PdfNumber(pdfDoc.getNextStructParentIndex()));
        }

        if (overlayFont == null) {
            System.err.println("  Warning: no overlay font — "
                + "cannot write form-image MCRs on page " + pageNum);
            return 0;
        }

        // 3. Read page rotation for coordinate transform
        int rotation = 0;
        PdfNumber rotObj = page.getPdfObject().getAsNumber(PdfName.Rotate);
        if (rotObj != null) rotation = rotObj.intValue();
        float mbWidth = page.getMediaBox().getWidth();
        float mbHeight = page.getMediaBox().getHeight();

        // 4. Write MCRs for all Docling elements on this page
        PdfCanvas canvas = ContentPreservingTagger.getAppendCanvas(
            page, pdfDoc);
        int count = 0;

        for (ContentPreservingTagger.ElementInfo ei : allElements) {
            if (ei.pageNo != pageNum) continue;
            int[] path = elementTreePath.get(ei.id);
            if (path == null) {
                // Container elements (L, Table, TOC) may not have a direct
                // tree path but their children do.  Don't skip them.
                if ("L".equals(ei.tag) || "Table".equals(ei.tag)
                        || "TOC".equals(ei.tag)) {
                    // Fall through to child handling below
                } else {
                    continue;
                }
            }

            // Tables: write each cell as MCR
            if ("Table".equals(ei.tag) && ei.cells != null
                    && !ei.cells.isEmpty()) {
                for (ContentPreservingTagger.CellInfo ci : ei.cells) {
                    int[] cellPath = elementTreePath.get(ci.cellId);
                    if (cellPath == null) continue;
                    String cellText = ci.text;
                    if (cellText == null || cellText.trim().isEmpty())
                        continue;
                    float x = ci.hasBbox ? ci.bboxL : (ei.hasBbox ? ei.bboxL : 72);
                    float y = ci.hasBbox ? ci.bboxB : (ei.hasBbox ? ei.bboxB : 72);
                    float[] cs = displayToContent(x, y, rotation, mbWidth, mbHeight);
                    try {
                        tagPointer.moveToRoot();
                        for (int idx : cellPath)
                            tagPointer.moveToKid(idx);
                        tagPointer.setPageForTagging(page);
                        PdfAccessibilityTagger.writeInvisibleMCR(
                            canvas, tagPointer, overlayFont,
                            cs[0], cs[1], cellText);
                        processedElems.add(ci.cellId);
                        count++;
                        tagPointer.moveToRoot();
                    } catch (Exception e) {
                        System.err.println("  Warning: form-image MCR "
                            + "write failed: " + e.getMessage());
                        try { tagPointer.moveToRoot(); }
                        catch (Exception e2) {
                            System.err.println("  Warning: tag pointer "
                                + "recovery failed — subsequent tags may "
                                + "nest incorrectly: " + e2.getMessage());
                        }
                    }
                }
                processedElems.add(ei.id);
                continue;
            }

            // Lists: write each LI as MCR
            if ("L".equals(ei.tag) && ei.listItems != null
                    && !ei.listItems.isEmpty()) {
                for (ContentPreservingTagger.ListItemInfo li : ei.listItems) {
                    int[] liPath = elementTreePath.get(li.liId);
                    if (liPath == null) continue;
                    String liText = li.text;
                    if (liText == null || liText.trim().isEmpty())
                        continue;
                    float x = li.hasBbox ? li.bboxL : (ei.hasBbox ? ei.bboxL : 72);
                    float y = li.hasBbox ? li.bboxB : (ei.hasBbox ? ei.bboxB : 72);
                    float[] cs = displayToContent(x, y, rotation, mbWidth, mbHeight);
                    try {
                        tagPointer.moveToRoot();
                        for (int idx : liPath)
                            tagPointer.moveToKid(idx);
                        tagPointer.setPageForTagging(page);
                        PdfAccessibilityTagger.writeInvisibleMCR(
                            canvas, tagPointer, overlayFont,
                            cs[0], cs[1], liText);
                        processedElems.add(li.liId);
                        count++;
                        tagPointer.moveToRoot();
                    } catch (Exception e) {
                        System.err.println("  Warning: form-image MCR "
                            + "write failed: " + e.getMessage());
                        try { tagPointer.moveToRoot(); }
                        catch (Exception e2) {
                            System.err.println("  Warning: tag pointer "
                                + "recovery failed — subsequent tags may "
                                + "nest incorrectly: " + e2.getMessage());
                        }
                    }
                }
                processedElems.add(ei.id);
                continue;
            }

            // TOC: write each TOCI as MCR
            if ("TOC".equals(ei.tag) && ei.tocItems != null
                    && !ei.tocItems.isEmpty()) {
                for (ContentPreservingTagger.TocItemInfo ti : ei.tocItems) {
                    int[] tiPath = elementTreePath.get(ti.tociId);
                    if (tiPath == null) continue;
                    String tiText = ti.text;
                    if (tiText == null || tiText.trim().isEmpty())
                        continue;
                    float x = ei.hasBbox ? ei.bboxL : 72;
                    float y = ei.hasBbox ? ei.bboxB : 72;
                    float[] cs = displayToContent(x, y, rotation, mbWidth, mbHeight);
                    try {
                        tagPointer.moveToRoot();
                        for (int idx : tiPath)
                            tagPointer.moveToKid(idx);
                        tagPointer.setPageForTagging(page);
                        PdfAccessibilityTagger.writeInvisibleMCR(
                            canvas, tagPointer, overlayFont,
                            cs[0], cs[1], tiText);
                        processedElems.add(ti.tociId);
                        count++;
                        tagPointer.moveToRoot();
                    } catch (Exception e) {
                        System.err.println("  Warning: form-image MCR "
                            + "write failed: " + e.getMessage());
                        try { tagPointer.moveToRoot(); }
                        catch (Exception e2) {
                            System.err.println("  Warning: tag pointer "
                                + "recovery failed — subsequent tags may "
                                + "nest incorrectly: " + e2.getMessage());
                        }
                    }
                }
                processedElems.add(ei.id);
                continue;
            }

            // Regular elements (P, H1, Figure, etc.)
            String mcrText = (ei.text != null && !ei.text.trim().isEmpty())
                ? ei.text : ei.altText;
            if (mcrText == null || mcrText.trim().isEmpty()) continue;
            float x = ei.hasBbox ? ei.bboxL : 72;
            float y = ei.hasBbox ? ei.bboxB : 72;
            float[] cs = displayToContent(x, y, rotation, mbWidth, mbHeight);
            try {
                tagPointer.moveToRoot();
                for (int idx : path)
                    tagPointer.moveToKid(idx);
                tagPointer.setPageForTagging(page);
                PdfAccessibilityTagger.writeInvisibleMCR(
                    canvas, tagPointer, overlayFont,
                    cs[0], cs[1], mcrText);
                processedElems.add(ei.id);
                count++;
                tagPointer.moveToRoot();
            } catch (Exception e) {
                System.err.println("  Warning: form-image MCR write "
                    + "failed: " + e.getMessage());
                try { tagPointer.moveToRoot(); }
                catch (Exception e2) {
                    System.err.println("  Warning: tag pointer recovery "
                        + "failed — subsequent tags may nest incorrectly: "
                        + e2.getMessage());
                }
            }
        }

        return count;
    }

    /**
     * Wrap original page content in a Figure structure element with /Alt,
     * instead of Artifact-wrapping it.  The form background image stays
     * visible and is properly tagged for PDF/UA compliance (7.20 t2).
     *
     * Uses PdfCanvas.openTag()/closeTag() to register the MCID with
     * iText's TagStructureContext, mirroring the pattern used by
     * wrapPageAsArtifact() but with a structure element instead of /Artifact.
     */
    private static void tagPageContentAsFigure(
        PdfDocument pdfDoc, PdfPage page,
        TagTreePointer tagPointer, String altText
    ) throws Exception {
        byte[] content = page.getContentBytes();
        if (content == null || content.length == 0) return;

        // Create Figure SE in the structure tree
        tagPointer.moveToRoot();
        tagPointer.addTag("Figure");
        PdfStructElem figElem = tagPointer.getContext()
            .getPointerStructElem(tagPointer);
        figElem.setAlt(new PdfString(altText, PdfEncodings.UNICODE_BIG));

        // Ensure StructParents on the page
        if (page.getPdfObject().get(new PdfName("StructParents")) == null) {
            page.getPdfObject().put(new PdfName("StructParents"),
                new PdfNumber(pdfDoc.getNextStructParentIndex()));
        }

        // Wrap page content in Figure BDC/EMC using PdfCanvas
        tagPointer.setPageForTagging(page);
        TagReference figRef = tagPointer.getTagReference();

        PdfCanvas canvas = new PdfCanvas(
            new PdfStream(), page.getResources(), pdfDoc);
        canvas.openTag(figRef);
        canvas.getContentStream().getOutputStream().writeBytes(content);
        canvas.getContentStream().getOutputStream().writeNewLine();
        canvas.closeTag();

        page.put(PdfName.Contents, canvas.getContentStream());

        tagPointer.moveToParent(); // back from Figure to parent
    }
}
