<?php
/**
 * Replies on a single post: the full thread, then the reply form.
 *
 * Imported Mastodon and Bluesky replies are threaded as comments, so this
 * is also where a social post's conversation is read in full; the archive's
 * inline preview (index.php) links here as "#comments".
 *
 * No avatars. Only the site's own users have one OnionPress serves
 * (onionpress-avatar.php); for anyone else get_avatar() asks Gravatar,
 * a clearnet request from every visitor's browser on every page view.
 *
 * @package OnionPress
 */

if (!defined('ABSPATH')) {
    exit;
}

// A password-protected post keeps its replies behind the same password.
if (post_password_required()) {
    return;
}
?>

<section id="comments" class="post comments-area">

    <?php if (have_comments()) : ?>
        <h2 class="comments-title">
            <?php
            $count = (int) get_comments_number();
            echo esc_html(sprintf(
                _n('%s reply', '%s replies', $count, 'onionpress'),
                number_format_i18n($count)
            ));
            ?>
        </h2>

        <ol class="comment-list">
            <?php
            wp_list_comments(array(
                'style'       => 'ol',
                'short_ping'  => true,
                'avatar_size' => 0,
                'reply_text'  => 'Reply',
            ));
            ?>
        </ol>

        <?php
        the_comments_navigation(array(
            'prev_text' => '&laquo; Older replies',
            'next_text' => 'Newer replies &raquo;',
        ));
        ?>
    <?php endif; ?>

    <?php if (!comments_open() && get_comments_number()) : ?>
        <p class="no-comments">Replies are closed.</p>
    <?php endif; ?>

    <?php
    comment_form(array(
        'title_reply'        => 'Leave a reply',
        'title_reply_to'     => 'Reply to %s',
        'title_reply_before' => '<h2 id="reply-title" class="comment-reply-title">',
        'title_reply_after'  => '</h2>',
        'label_submit'       => 'Post reply',
        'class_submit'       => 'comment-submit',
    ));
    ?>

</section>
