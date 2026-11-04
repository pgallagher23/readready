// SPDX-License-Identifier: AGPL-3.0-or-later
package pdfaccessibility;

import java.io.File;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;

/** Resolves the user font directory and indexes its filenames case-insensitively. */
final class FontDirIndex {
    static final String PROPERTY = "readready.fontDir";
    static final String ENV = "READREADY_FONT_DIR";
    private static final Map<String, FontDirIndex> CACHE = new HashMap<>();
    private static String resolvedDir;

    private final Map<String, String> byLower = new HashMap<>();

    private FontDirIndex(File dir) {
        File[] files = dir.listFiles();
        if (files == null) return;
        for (File f : files) {
            if (f.isFile()) byLower.putIfAbsent(f.getName().toLowerCase(Locale.ROOT), f.getAbsolutePath());
        }
    }

    static synchronized FontDirIndex of(String dirPath) {
        return CACHE.computeIfAbsent(dirPath, p -> new FontDirIndex(new File(p)));
    }

    /** Real path for a filename, matched case-insensitively; null when absent. */
    String resolve(String fileName) {
        return byLower.get(fileName.toLowerCase(Locale.ROOT));
    }

    /** -Dreadready.fontDir, else $READREADY_FONT_DIR, else ../fonts/ next to the JAR's directory. */
    static synchronized String resolveFontDir() {
        if (resolvedDir != null) return resolvedDir;
        String p = System.getProperty(PROPERTY);
        if (p == null || p.isEmpty()) p = System.getenv(ENV);
        if (p == null || p.isEmpty()) {
            try {
                String jar = PdfAccessibilityTagger.class.getProtectionDomain()
                    .getCodeSource().getLocation().toURI().getPath();
                p = new File(jar).getParentFile().getParent() + "/fonts";
            } catch (Exception e) {
                p = "fonts";
            }
        }
        resolvedDir = p.endsWith("/") ? p : p + "/";
        if (!new File(resolvedDir).isDirectory()) {
            System.err.println("[tagger] font directory not found: " + resolvedDir
                + " — metric-matched fonts unavailable, using system fallbacks");
        }
        return resolvedDir;
    }

    static synchronized void clearCache() { CACHE.clear(); resolvedDir = null; }
}
