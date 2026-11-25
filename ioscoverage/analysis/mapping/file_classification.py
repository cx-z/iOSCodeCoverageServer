"""Native-runtime coverage excludes known project metadata and resource data files."""

from pathlib import PurePosixPath


NON_INSTRUMENTED_SUFFIXES = frozenset({
    '.md', '.pbxproj', '.strings', '.stringsdict', '.xcstrings',
    '.plist', '.json', '.xcconfig', '.entitlements', '.xcprivacy',
    '.xib', '.storyboard', '.xcscheme', '.xcworkspacedata', '.xcsettings', '.xctestplan', '.xcfilelist',
    '.txt', '.csv', '.tsv', '.xml', '.yaml', '.yml', '.toml', '.ini',
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.heic', '.avif', '.bmp', '.tif', '.tiff', '.ico', '.icns',
    '.svg', '.pdf', '.html', '.htm', '.css', '.rtf', '.ktx', '.ktx2', '.pvr', '.astc', '.car', '.ttf', '.otf', '.ttc', '.woff', '.woff2',
    '.mp3', '.m4a', '.aac', '.wav', '.aiff', '.caf', '.flac', '.ogg',
    '.mp4', '.mov', '.m4v', '.webm', '.usdz', '.scn', '.sks',
})
NON_INSTRUMENTED_NAMES = frozenset({'Podfile', 'Podfile.lock', 'Package.resolved', 'Cartfile.resolved',
                                        '.gitignore', '.gitattributes', '.gitmodules', '.xccurrentversion'})


def is_non_instrumented_file(path):
    """Recognizes configuration, localization, interface and media/resource data.

    Asset-catalog manifests are JSON. Binary diffs have no changed source lines.
    Executable files (including Swift, JavaScript and Metal) are never excluded
    merely because they live under Resources, a bundle or an asset directory.
    """

    path = PurePosixPath(path)
    model_contents = path.name == 'contents' and any(
        part.endswith(('.xcdatamodel', '.xcmappingmodel')) for part in path.parts[:-1])
    return model_contents or path.suffix.lower() in NON_INSTRUMENTED_SUFFIXES or path.name in NON_INSTRUMENTED_NAMES
