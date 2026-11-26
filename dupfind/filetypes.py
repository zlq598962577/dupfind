"""按扩展名把文件与分组归入用户能理解的类别。

报告里混着两类东西：用户自己的文件（文档、照片、视频、压缩包）与程序/开发
产物（源码、配置、二进制）。前者通常值得清理，后者多半是「别的程序的文件」，
误删会破坏软件。这里只做**分类**，不做判决：视图默认只展示用户文件类别，
其余类别仍可一键切换查看，扫描与处置规则不受影响。
"""

from __future__ import annotations

import ntpath
from collections.abc import Iterable

CATEGORIES: tuple[str, ...] = (
    "image",
    "video",
    "audio",
    "document",
    "archive",
    "installer",
    "font",
    "other",
    "code",
    "config",
    "program",
)

CATEGORY_LABELS: dict[str, str] = {
    "image": "图片",
    "video": "视频",
    "audio": "音频",
    "document": "文档",
    "archive": "压缩包",
    "installer": "安装包",
    "font": "字体",
    "other": "其他",
    "code": "代码",
    "config": "配置",
    "program": "程序",
}

#: 默认展示的类别。代码 / 配置 / 程序是用户抱怨的「其他程序的文件」，
#: 默认隐藏；「其他」无法判断归属，按「宁可多显示」保留。
USER_CATEGORIES: frozenset[str] = frozenset(
    {"image", "video", "audio", "document", "archive", "installer", "font", "other"}
)

#: 组内类别不一致时的取舍顺序：只要有一份属于用户文件，就按用户文件展示。
CATEGORY_PRIORITY: tuple[str, ...] = (
    "image",
    "video",
    "audio",
    "document",
    "archive",
    "installer",
    "font",
    "other",
    "code",
    "config",
    "program",
)

_EXTENSION_GROUPS: dict[str, tuple[str, ...]] = {
    "image": (
        ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".webp", ".heic", ".heif",
        ".tif", ".tiff", ".svg", ".ico", ".raw", ".cr2", ".cr3", ".nef", ".arw",
        ".dng", ".orf", ".rw2", ".psd", ".ai", ".sketch", ".fig",
    ),
    "video": (
        ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg",
        ".mpeg", ".m2ts", ".rmvb", ".rm", ".3gp", ".vob", ".ogv",
    ),
    "audio": (
        ".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma", ".ape", ".opus",
        ".aiff", ".mid", ".midi", ".amr",
    ),
    "document": (
        ".doc", ".docx", ".xls", ".xlsx", ".xlsm", ".ppt", ".pptx", ".pdf",
        ".wps", ".et", ".dps", ".rtf", ".odt", ".ods", ".odp", ".epub", ".mobi",
        ".azw3", ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".pages",
        ".numbers", ".key", ".one", ".vsd", ".vsdx",
    ),
    "archive": (
        ".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".zst",
        ".iso", ".img", ".cab", ".arj", ".lzh", ".lz4", ".lzma",
    ),
    "installer": (
        ".msi", ".msix", ".msixbundle", ".appx", ".appxbundle", ".apk", ".ipa",
        ".deb", ".rpm", ".dmg", ".pkg",
    ),
    "font": (".ttf", ".otf", ".woff", ".woff2", ".ttc", ".fon", ".eot"),
    "code": (
        ".py", ".pyw", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
        # .ts 在开发目录里几乎总是 TypeScript 源码；MPEG-TS 视频通常用
        # .mp4/.mkv 存放，误判的代价只是用户切到「全部类型」再看一眼。

        ".vue", ".svelte", ".java", ".kt", ".kts", ".groovy", ".scala", ".c",
        ".h", ".hh", ".hpp", ".hxx", ".cc", ".cpp", ".cxx", ".cs", ".go", ".rs",
        ".rb", ".php", ".swift", ".m", ".mm", ".lua", ".pl", ".pm", ".r", ".jl",
        ".dart", ".ex", ".exs", ".erl", ".hrl", ".clj", ".cljs", ".fs", ".fsx",
        ".vb", ".asm", ".s", ".sh", ".bash", ".zsh", ".fish", ".bat", ".cmd",
        ".ps1", ".psm1", ".sql", ".graphql", ".proto", ".sol", ".v",
        ".sv", ".vhdl", ".ipynb", ".qml", ".nim", ".zig",
    ),
    "config": (
        ".json", ".jsonc", ".json5", ".xml", ".yaml", ".yml", ".toml", ".ini",
        ".cfg", ".conf", ".config", ".properties", ".env", ".reg", ".plist",
        ".lock", ".map", ".gradle", ".cmake", ".mk", ".sln", ".csproj", ".vcxproj",
        ".psd1",
        ".pbxproj", ".xcconfig", ".editorconfig", ".npmrc", ".yarnrc", ".gitignore",
        ".gitattributes", ".dockerignore", ".htaccess", ".manifest", ".desktop",
        ".service", ".spec", ".dtd", ".xsd", ".xsl", ".xslt",
    ),
    "program": (
        ".dll", ".exe", ".sys", ".drv", ".ocx", ".cpl", ".scr", ".so", ".dylib",
        ".pak", ".jar", ".war", ".ear", ".class", ".pyc", ".pyo",
        ".pyd", ".node", ".wasm", ".lib", ".obj", ".o", ".a", ".res",
        ".mui", ".pdb", ".ilk", ".exp", ".tlb", ".gpd",
    ),
}

_EXTENSION_TO_CATEGORY: dict[str, str] = {
    extension: category
    for category, extensions in _EXTENSION_GROUPS.items()
    for extension in extensions
}

#: 无扩展名但能确定归属的特殊文件名。
_NAME_TO_CATEGORY: dict[str, str] = {
    "makefile": "code",
    "gnumakefile": "code",
    "dockerfile": "code",
    "vagrantfile": "code",
    "jenkinsfile": "code",
    "cmakelists.txt": "code",
    ".gitignore": "config",
    ".gitattributes": "config",
    ".dockerignore": "config",
    ".env": "config",
    ".editorconfig": "config",
    "license": "document",
    "licence": "document",
    "readme": "document",
    "changelog": "document",
}


def category_for_name(name: str) -> str:
    """按文件名返回类别；未知扩展名归入 ``other``（默认可见）。"""
    text = str(name or "").strip()
    if not text:
        return "other"
    lowered = text.lower()
    special = _NAME_TO_CATEGORY.get(lowered)
    if special is not None:
        return special
    extension = ntpath.splitext(lowered)[1]
    if not extension:
        return "other"
    return _EXTENSION_TO_CATEGORY.get(extension, "other")


def category_for_path(path: str) -> str:
    return category_for_name(ntpath.basename(str(path)))


def group_category(paths: Iterable[str]) -> str:
    """整组的类别：任一文件属于用户文件类别，即按用户文件归类。

    组内文件内容相同、扩展名可能不同（``照片.jpg`` 与 ``照片.jpeg``）；只要
    有一份像用户文件，就倾向于让用户看见，而不是因为一份 ``.dat`` 把它藏掉。
    """
    categories = {category_for_path(path) for path in paths}
    for category in CATEGORY_PRIORITY:
        if category in categories:
            return category
    return "other"


def is_user_category(category: str) -> bool:
    return category in USER_CATEGORIES


def top_dir(path: str, root: str) -> str:
    """相对扫描根的第一层目录；直接位于根下时返回“(根目录)”。

    目录面板按这一层聚合：再往下拆会碎成几千条，再往上收会把用户的
    Downloads / Documents / 某软件的缓存目录混成一个。
    """
    normalized_root = ntpath.normpath(str(root or ""))
    normalized_path = ntpath.normpath(str(path or ""))
    try:
        relative = ntpath.relpath(normalized_path, normalized_root)
    except ValueError:
        relative = normalized_path
    # 只把真正的父级跳转（".." 或 "..\\"）当成越界；Windows 允许名为
    # "..cache" 的合法目录，不能因为它以两个点开头就并进扫描根
    if (
        relative == ".."
        or relative.startswith(".." + ntpath.sep)
        or ntpath.isabs(relative)
        or relative == "."
    ):
        return "(根目录)"
    head = relative.split(ntpath.sep, 1)[0]
    if head == relative:
        # 路径本身就是根下的文件
        return "(根目录)"
    return head
