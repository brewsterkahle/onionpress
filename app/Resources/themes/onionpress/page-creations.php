<?php
/**
 * Template Name: My Creations
 *
 * Displays files from the My Creations directory with folder navigation.
 * Requires the OnionPress Creations plugin.
 */

get_header();

$current_folder = isset($_GET['folder']) ? sanitize_text_field($_GET['folder']) : '';
$contents = class_exists('OnionPress_Creations')
    ? OnionPress_Creations::get_folder_contents($current_folder)
    : array('folders' => array(), 'files' => array());
$page_url = get_permalink();
$upload_limits = (class_exists('OnionPress_Creations') && method_exists('OnionPress_Creations', 'upload_limits'))
    ? OnionPress_Creations::upload_limits()
    : array('max_files' => 0, 'max_bytes' => 0);
?>

<article class="post">
    <h1 class="post-title"><?php the_title(); ?></h1>

    <?php if (class_exists('OnionPress_Creations') && OnionPress_Creations::can_upload()) : ?>
        <div class="creations-upload">
            <form method="post" enctype="multipart/form-data" id="creations-upload-form"
                  data-max-files="<?php echo esc_attr($upload_limits['max_files']); ?>"
                  data-max-bytes="<?php echo esc_attr($upload_limits['max_bytes']); ?>">
                <?php wp_nonce_field('creations_upload', 'creations_upload_nonce'); ?>
                <?php if ($current_folder) : ?>
                    <input type="hidden" name="creations_upload_folder" value="<?php echo esc_attr($current_folder); ?>">
                <?php endif; ?>
                <input type="hidden" name="creations_upload_count" value="0" id="creations-upload-count">
                <label class="creations-upload-area" id="creations-drop-area">
                    <input type="file" name="creations_upload[]" multiple style="display:none" id="creations-file-input">
                    <span class="creations-upload-text" id="creations-upload-text">Drop files here or click to upload</span>
                </label>
            </form>
        </div>
        <script>
        (function() {
            var area = document.getElementById('creations-drop-area');
            var input = document.getElementById('creations-file-input');
            var form = document.getElementById('creations-upload-form');
            var countField = document.getElementById('creations-upload-count');
            var text = document.getElementById('creations-upload-text');
            var maxFiles = parseInt(form.getAttribute('data-max-files'), 10) || 0;
            var maxBytes = parseInt(form.getAttribute('data-max-bytes'), 10) || 0;
            // PHP drops files past max_file_uploads without a word, and a
            // request over post_max_size arrives empty: say so before
            // sending anything. The server reports what still goes wrong.
            function send() {
                var files = input.files, total = 0;
                if (!files.length) return;
                for (var i = 0; i < files.length; i++) total += files[i].size;
                if (maxFiles && files.length > maxFiles) {
                    text.textContent = files.length + ' files is more than ' + maxFiles +
                        ' at once. Nothing was uploaded; please upload them in smaller groups.';
                    input.value = '';
                    return;
                }
                if (maxBytes && total > maxBytes) {
                    text.textContent = 'These files are larger together than the ' +
                        Math.floor(maxBytes / 1048576) + ' MB upload limit. Nothing was uploaded; please upload fewer at a time.';
                    input.value = '';
                    return;
                }
                countField.value = files.length;
                text.textContent = 'Uploading ' + files.length + (files.length === 1 ? ' file' : ' files') + '...';
                form.submit();
            }
            area.addEventListener('dragover', function(e) { e.preventDefault(); area.classList.add('dragover'); });
            area.addEventListener('dragleave', function() { area.classList.remove('dragover'); });
            area.addEventListener('drop', function(e) {
                e.preventDefault();
                area.classList.remove('dragover');
                input.files = e.dataTransfer.files;
                send();
            });
            input.addEventListener('change', send);
        })();
        </script>
    <?php endif; ?>

    <?php // Breadcrumb navigation ?>
    <?php if ($current_folder) : ?>
        <nav class="creations-breadcrumb">
            <a href="<?php echo esc_url($page_url); ?>">My Creations</a>
            <?php
            $parts = explode('/', $current_folder);
            $path_so_far = '';
            foreach ($parts as $i => $part) :
                $path_so_far .= ($path_so_far ? '/' : '') . $part;
                if ($i < count($parts) - 1) : ?>
                    <span class="breadcrumb-sep">/</span>
                    <a href="<?php echo esc_url(add_query_arg('folder', urlencode($path_so_far), $page_url)); ?>"><?php echo esc_html($part); ?></a>
                <?php else : ?>
                    <span class="breadcrumb-sep">/</span>
                    <span class="breadcrumb-current"><?php echo esc_html($part); ?></span>
                <?php endif;
            endforeach; ?>
        </nav>
    <?php endif; ?>

    <?php if (empty($contents['folders']) && empty($contents['files'])) : ?>
        <div class="creations-empty">
            <p>No creations yet.</p>
            <?php if (class_exists('OnionPress_Creations') && OnionPress_Creations::can_upload()) : ?>
                <p>Upload files above or drop them into <code>~/OnionPress/Creations/My Creations/</code></p>
            <?php endif; ?>
        </div>
    <?php else : ?>

        <div class="creations-grid" id="creations-grid">
            <?php // Folders first ?>
            <?php foreach ($contents['folders'] as $folder) : ?>
                <a href="<?php echo esc_url(add_query_arg('folder', urlencode($folder['rel_path']), $page_url)); ?>" class="creation-item creation-folder-item">
                    <div class="creation-thumbnail">
                        <span class="creation-icon">&#x1f4c1;</span>
                    </div>
                    <div class="creation-info">
                        <div class="creation-name"><?php echo esc_html($folder['name']); ?></div>
                        <div class="creation-meta"><?php echo $folder['count']; ?> item<?php echo $folder['count'] !== 1 ? 's' : ''; ?></div>
                    </div>
                </a>
            <?php endforeach; ?>

            <?php // Then files ?>
            <?php foreach ($contents['files'] as $file) : ?>
                <?php if (!empty($file['is_video'])) : ?>
                    <div class="creation-item creation-video-item">
                        <video class="creation-thumbnail" controls preload="none"
                               <?php if (!empty($file['thumb_url'])) : ?>poster="<?php echo esc_url($file['thumb_url']); ?>"<?php endif; ?>>
                            <source src="<?php echo esc_url($file['url']); ?>">
                        </video>
                        <div class="creation-info">
                            <div class="creation-name"><?php echo esc_html($file['name']); ?></div>
                            <div class="creation-meta"><?php echo OnionPress_Creations::format_size($file['size']); ?></div>
                        </div>
                    </div>
                <?php else : ?>
                    <a href="<?php echo esc_url($file['url']); ?>" class="creation-item">
                        <?php if (!empty($file['thumb_url'])) : ?>
                            <img class="creation-thumbnail" src="<?php echo esc_url($file['thumb_url']); ?>" alt="<?php echo esc_attr($file['name']); ?>">
                        <?php elseif ($file['is_image']) : ?>
                            <img class="creation-thumbnail" src="<?php echo esc_url($file['url']); ?>" alt="<?php echo esc_attr($file['name']); ?>">
                        <?php else : ?>
                            <div class="creation-thumbnail">
                                <span class="creation-icon"><?php echo $file['icon']; ?></span>
                            </div>
                        <?php endif; ?>
                        <div class="creation-info">
                            <div class="creation-name"><?php echo esc_html($file['name']); ?></div>
                            <div class="creation-meta"><?php echo OnionPress_Creations::format_size($file['size']); ?></div>
                        </div>
                    </a>
                <?php endif; ?>
            <?php endforeach; ?>
        </div>
    <?php endif; ?>
</article>

<?php get_footer(); ?>
