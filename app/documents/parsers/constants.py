TEXT_LIKE_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",    
    ".yaml",
    ".yml",
    ".json",
    ".py",
    ".sh",
    ".bash",
    ".sql",
    ".xml",
    ".html",
    ".htm",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".js",
    ".ts",
}


HWP_EXTENSIONS = {
    ".hwpx",
    ".hwp",
}


BINARY_DOC_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".bmp",
    ".tiff",
    ".gif",
}


def guess_code_fence_language(ext: str) -> str:
    mapping = {
        ".py": "python",
        ".sh": "bash",
        ".bash": "bash",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".json": "json",
        ".sql": "sql",
        ".xml": "xml",
        ".html": "html",
        ".htm": "html",
        ".js": "javascript",
        ".ts": "typescript",
        ".toml": "toml",
        ".ini": "ini",
        ".cfg": "ini",
        ".conf": "ini",
    }
    return mapping.get(ext, "")
