<?php
/**
 * Plugin Name: OnionPress Status Page
 * Description: Public system health status page at /onionpress-status.
 *              Reads status data from the shared volume and optionally
 *              displays WP Statistics traffic summary. Auto-refreshes via AJAX.
 * Version:     1.1
 * Network:     true
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit;
}

/**
 * JSON API endpoint for live status polling.
 * Append ?refresh=1 to also trigger a full status rewrite on the next watcher tick.
 */
add_action( 'parse_request', function () {
    $path = trim( parse_url( $_SERVER['REQUEST_URI'], PHP_URL_PATH ), '/' );
    if ( $path !== 'onionpress-status-json' ) {
        return;
    }

    // Request a status rewrite if data is stale (>30s) or explicitly requested
    $status_file = '/var/lib/onionpress/status.json';
    $stale = true;
    if ( file_exists( $status_file ) ) {
        $age = time() - filemtime( $status_file );
        $stale = ( $age > 30 );
    }
    if ( $stale || ! empty( $_GET['refresh'] ) ) {
        @file_put_contents( '/var/lib/onionpress/requested-action', 'refresh-status' );
    }

    header( 'Content-Type: application/json; charset=utf-8' );
    header( 'Cache-Control: no-cache, no-store, must-revalidate' );
    header( 'X-Robots-Tag: noindex' );

    $result = array( 'status' => null, 'logs' => '' );

    if ( file_exists( $status_file ) ) {
        $raw = file_get_contents( $status_file );
        if ( $raw !== false ) {
            $result['status'] = json_decode( $raw, true );
        }
    }

    $logs_file = '/var/lib/onionpress/recent-logs.txt';
    if ( file_exists( $logs_file ) ) {
        $result['logs'] = file_get_contents( $logs_file );
    }

    echo json_encode( $result );
    exit;
} );

/**
 * Intercept requests to /onionpress-status and render the status page.
 */
add_action( 'parse_request', function () {
    $path = trim( parse_url( $_SERVER['REQUEST_URI'], PHP_URL_PATH ), '/' );
    if ( $path !== 'onionpress-status' ) {
        return;
    }

    // Read status.json from the shared volume
    $status_file = '/var/lib/onionpress/status.json';
    $status = null;
    if ( file_exists( $status_file ) ) {
        $raw = file_get_contents( $status_file );
        if ( $raw !== false ) {
            $status = json_decode( $raw, true );
        }
    }

    // Read logs
    $logs = '';
    $logs_file = '/var/lib/onionpress/recent-logs.txt';
    if ( file_exists( $logs_file ) ) {
        $logs = trim( file_get_contents( $logs_file ) );
    }

    // Gather WP Statistics data if the plugin is active
    $wp_stats = null;
    if ( function_exists( 'wp_statistics_pages' ) ) {
        $wp_stats = array(
            'total_views'    => (int) wp_statistics_pages( 'total', null, -1 ),
            'total_visitors' => (int) wp_statistics_visitor( 'total', null, true ),
            'today_views'    => (int) wp_statistics_pages( 'today', null, -1 ),
            'today_visitors' => (int) wp_statistics_visitor( 'today', null, true ),
        );

        // Daily page views for last 30 days (for the graph)
        $daily_views = array();
        for ( $i = 29; $i >= 0; $i-- ) {
            $date = date( 'Y-m-d', strtotime( "-{$i} days" ) );
            $count = (int) wp_statistics_pages( $date, null, -1 );
            $daily_views[] = array( 'date' => $date, 'views' => $count );
        }
        $wp_stats['daily_views'] = $daily_views;

        // Top 5 pages (use database directly for efficiency)
        global $wpdb;
        $table = $wpdb->prefix . 'statistics_pages';
        if ( $wpdb->get_var( "SHOW TABLES LIKE '{$table}'" ) === $table ) {
            $top_pages = $wpdb->get_results(
                "SELECT uri, SUM(count) as total FROM {$table} GROUP BY uri ORDER BY total DESC LIMIT 5",
                ARRAY_A
            );
            $wp_stats['top_pages'] = $top_pages ? $top_pages : array();
        }
    }

    header( 'Content-Type: text/html; charset=utf-8' );
    header( 'Cache-Control: no-cache, no-store, must-revalidate' );
    header( 'X-Robots-Tag: noindex' );

    onionpress_render_status_page( $status, $wp_stats, $logs );
    exit;
} );

/**
 * Render the standalone status page HTML.
 */
function onionpress_render_status_page( $status, $wp_stats, $logs ) {
    $state         = $status ? ( $status['state'] ?? 'unknown' ) : 'unknown';
    $version       = $status ? ( $status['version'] ?? '?' ) : '?';
    $onion_address = $status ? ( $status['onion_address'] ?? '' ) : '';
    $uptime        = $status ? ( $status['uptime_seconds'] ?? 0 ) : 0;
    $bootstrap     = $status ? ( $status['bootstrap_pct'] ?? 0 ) : 0;
    $containers    = $status ? ( $status['containers'] ?? array() ) : array();
    $wayback_queue = $status ? ( $status['wayback_queue_count'] ?? 0 ) : 0;
    $updated_at    = $status ? ( $status['updated_at'] ?? '' ) : '';
    $oh            = $status ? ( $status['onionheaven'] ?? array() ) : array();
    $load_avg      = $status ? ( $status['load_avg'] ?? array() ) : array();
    $host_uptime   = $status ? ( $status['host_uptime_seconds'] ?? 0 ) : 0;
    // C Tor is the only implementation since 2026-09-24 (Arti was removed);
    // the field stays in the status JSON for the OnionHome schema.
    $tor_impl      = $status ? ( $status['tor_impl'] ?? 'tor' ) : 'tor';
    $tor_impl_label = $tor_impl === 'tor' ? 'C Tor' : $tor_impl;

    // State color
    $state_colors = array(
        'running'  => '#8b5cf6', // purple
        'starting' => '#eab308', // yellow
        'stopped'  => '#6b7280', // gray
        'offline'  => '#6b7280',
        'stuck'    => '#ef4444', // red
        'unknown'  => '#6b7280',
    );
    $state_color = $state_colors[ $state ] ?? '#6b7280';

    // Format uptime
    $uptime_str = '';
    if ( $uptime > 0 ) {
        $days    = floor( $uptime / 86400 );
        $hours   = floor( ( $uptime % 86400 ) / 3600 );
        $minutes = floor( ( $uptime % 3600 ) / 60 );
        if ( $days > 0 ) {
            $uptime_str = "{$days}d {$hours}h {$minutes}m";
        } elseif ( $hours > 0 ) {
            $uptime_str = "{$hours}h {$minutes}m";
        } else {
            $uptime_str = "{$minutes}m";
        }
    }

    // Format host uptime
    $host_uptime_str = '';
    if ( $host_uptime > 0 ) {
        $hd = floor( $host_uptime / 86400 );
        $hh = floor( ( $host_uptime % 86400 ) / 3600 );
        $hm = floor( ( $host_uptime % 3600 ) / 60 );
        if ( $hd > 0 ) {
            $host_uptime_str = "{$hd}d {$hh}h {$hm}m";
        } elseif ( $hh > 0 ) {
            $host_uptime_str = "{$hh}h {$hm}m";
        } else {
            $host_uptime_str = "{$hm}m";
        }
    }

    // Format load averages
    $load_str = '';
    if ( ! empty( $load_avg ) ) {
        $load_str = implode( ' / ', array_map( function( $v ) { return number_format( $v, 2 ); }, array_slice( $load_avg, 0, 3 ) ) );
    }

    // Build daily views SVG graph
    $graph_svg = '';
    if ( $wp_stats && ! empty( $wp_stats['daily_views'] ) ) {
        $graph_svg = onionpress_render_traffic_graph( $wp_stats['daily_views'] );
    }

    ?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OnionPress Status</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background: #0f0f0f;
        color: #e5e5e5;
        padding: 2rem;
        max-width: 720px;
        margin: 0 auto;
    }
    h1 { font-size: 1.5rem; margin-bottom: 1.5rem; color: #fff; display: flex; align-items: center; justify-content: space-between; }
    h1 span { font-size: 0.75rem; color: #888; font-weight: normal; margin-left: 0.5rem; }
    .live-dot { font-size: 0.7rem; color: #555; }
    .card {
        background: #1a1a1a;
        border: 1px solid #333;
        border-radius: 8px;
        padding: 1.25rem;
        margin-bottom: 1rem;
    }
    .card h2 { font-size: 0.875rem; color: #888; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.75rem; }
    .status-grid {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
        gap: 1rem;
    }
    .status-stat .label { font-size: 0.6875rem; text-transform: uppercase; color: #666; letter-spacing: 0.05em; margin-bottom: 0.25rem; }
    .status-stat .value { font-size: 0.9375rem; font-weight: 600; color: #e5e5e5; }
    .state-dot {
        display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 0.4rem; vertical-align: middle;
    }
    .onion-row {
        display: flex; align-items: center; gap: 8px;
        margin-top: 1rem; padding-top: 1rem; border-top: 1px solid #262626;
    }
    .onion-addr {
        font-family: "SF Mono", "Fira Code", "Consolas", monospace;
        font-size: 0.75rem;
        word-break: break-all;
        color: #c084fc;
    }
    .copy-btn {
        padding: 2px 8px; font-size: 11px; cursor: pointer;
        border: 1px solid #555; border-radius: 3px; background: #262626; color: #c084fc; white-space: nowrap;
    }
    .copy-btn:hover { background: #333; }
    .refresh-btn {
        padding: 4px 12px; font-size: 12px; cursor: pointer;
        border: 1px solid #555; border-radius: 4px; background: #262626; color: #c084fc; white-space: nowrap;
    }
    .refresh-btn:hover { background: #333; }
    .refresh-btn:disabled { opacity: 0.5; cursor: default; }
    .logs-wrap { position: relative; }
    .most-recent-btn {
        display: none; position: absolute; bottom: 12px; right: 16px;
        padding: 4px 12px; font-size: 11px; cursor: pointer;
        border: 1px solid #555; border-radius: 4px; background: rgba(38,38,38,0.9); color: #c084fc;
        z-index: 1;
    }
    .most-recent-btn:hover { background: #333; }
    .containers-wrap { margin-top: 1rem; padding-top: 1rem; border-top: 1px solid #262626; }
    .containers-wrap .label { font-size: 0.6875rem; text-transform: uppercase; color: #666; letter-spacing: 0.05em; margin-bottom: 0.5rem; }
    .container-badges { display: flex; gap: 0.5rem; flex-wrap: wrap; }
    .container-badge {
        font-size: 0.75rem; padding: 2px 10px; border-radius: 4px; border: 1px solid #333;
    }
    .container-badge.ok { color: #4ade80; border-color: #166534; background: rgba(74,222,128,0.08); }
    .container-badge.bad { color: #f87171; border-color: #7f1d1d; background: rgba(248,113,113,0.08); }
    .stat-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 0.75rem; }
    .stat-box { text-align: center; }
    .stat-box .num { font-size: 1.5rem; font-weight: 700; color: #fff; }
    .stat-box .lbl { font-size: 0.75rem; color: #888; margin-top: 0.125rem; }
    .top-pages { list-style: none; }
    .top-pages li { display: flex; justify-content: space-between; padding: 0.25rem 0; font-size: 0.8125rem; }
    .top-pages li + li { border-top: 1px solid #262626; }
    .top-pages .pg-uri { color: #c084fc; max-width: 80%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .top-pages .pg-count { color: #888; }
    .graph-wrap { margin-top: 0.5rem; }
    .no-data { color: #555; font-size: 0.875rem; text-align: center; padding: 2rem 0; }
    .log-pre {
        background: #111; border: 1px solid #333; border-radius: 8px;
        padding: 1rem; font-family: "SF Mono", "Fira Code", "Consolas", monospace;
        font-size: 0.75rem; color: #aaa; max-height: 400px; overflow: auto;
        white-space: pre-wrap; word-break: break-all; line-height: 1.5;
    }
</style>
</head>
<body>
<h1>
    <span>OnionPress Status <span>v<span id="op-version"><?php echo esc_html( $version ); ?></span></span></span>
    <span style="display:flex;align-items:center;gap:10px;">
        <span class="live-dot" id="op-live-dot">Auto Updated <script>document.write(new Date().toLocaleTimeString())</script></span>
        <button type="button" class="refresh-btn" id="op-refresh" onclick="window.opRefresh(this)">Refresh Now</button>
    </span>
</h1>

<?php if ( ! $status ) : ?>
    <div class="card"><p class="no-data">Status data not available yet. The system may still be starting up.</p></div>
<?php else : ?>

<div class="card">
    <h2>System Status</h2>
    <div class="status-grid">
        <div class="status-stat">
            <div class="label">State</div>
            <div class="value" id="op-state"><span class="state-dot" style="background:<?php echo $state_color; ?>"></span><?php echo esc_html( ucfirst( $state ) ); ?></div>
        </div>
        <div class="status-stat">
            <div class="label">Version</div>
            <div class="value" id="op-version2"><?php echo esc_html( $version ); ?></div>
        </div>
        <div class="status-stat">
            <div class="label">Uptime</div>
            <div class="value" id="op-uptime"><?php echo $uptime_str ? esc_html( $uptime_str ) : '&mdash;'; ?></div>
        </div>
        <div class="status-stat">
            <div class="label">Tor Bootstrap</div>
            <div class="value" id="op-bootstrap"><?php echo (int) $bootstrap; ?>%</div>
        </div>
        <div class="status-stat">
            <div class="label">Tor Implementation</div>
            <div class="value" id="op-tor-impl"><?php echo esc_html( $tor_impl_label ); ?></div>
        </div>
        <div class="status-stat" id="op-wayback-row" <?php if ( $wayback_queue <= 0 ) echo 'style="display:none"'; ?>>
            <div class="label">Wayback Queue</div>
            <div class="value" id="op-wayback"><?php echo (int) $wayback_queue; ?> item<?php echo $wayback_queue != 1 ? 's' : ''; ?></div>
        </div>
        <?php if ( $load_str ) : ?>
        <div class="status-stat">
            <div class="label">Load Average</div>
            <div class="value" id="op-load"><?php echo esc_html( $load_str ); ?></div>
        </div>
        <?php endif; ?>
        <?php if ( $host_uptime_str ) : ?>
        <div class="status-stat">
            <div class="label">Host Uptime</div>
            <div class="value" id="op-host-uptime"><?php echo esc_html( $host_uptime_str ); ?></div>
        </div>
        <?php endif; ?>
    </div>

    <div class="onion-row" id="op-onion-row" <?php if ( ! $onion_address || strpos( $onion_address, '.onion' ) === false ) echo 'style="display:none"'; ?>>
        <span class="onion-addr" id="op-addr"><?php echo esc_html( $onion_address ); ?></span>
        <button type="button" class="copy-btn" onclick="navigator.clipboard.writeText(document.getElementById('op-addr').textContent).then(function(){var b=event.target;b.textContent='Copied!';setTimeout(function(){b.textContent='Copy'},1500)})">Copy</button>
    </div>

    <div class="containers-wrap" id="op-containers-wrap" <?php if ( empty( $containers ) ) echo 'style="display:none"'; ?>>
        <div class="label">Containers</div>
        <div class="container-badges" id="op-containers">
            <?php foreach ( $containers as $name => $cstate ) : ?>
            <span class="container-badge <?php echo $cstate === 'running' ? 'ok' : 'bad'; ?>">
                <?php echo esc_html( str_replace( 'onionpress-', '', $name ) ); ?>: <?php echo esc_html( $cstate ); ?>
            </span>
            <?php endforeach; ?>
        </div>
    </div>
</div>

<?php if ( $wp_stats ) : ?>
<div class="card">
    <h2>Traffic</h2>
    <div class="stat-grid">
        <div class="stat-box">
            <div class="num"><?php echo number_format( $wp_stats['total_views'] ); ?></div>
            <div class="lbl">Total Views</div>
        </div>
        <div class="stat-box">
            <div class="num"><?php echo number_format( $wp_stats['total_visitors'] ); ?></div>
            <div class="lbl">Total Visitors</div>
        </div>
        <div class="stat-box">
            <div class="num"><?php echo number_format( $wp_stats['today_views'] ); ?></div>
            <div class="lbl">Views Today</div>
        </div>
        <div class="stat-box">
            <div class="num"><?php echo number_format( $wp_stats['today_visitors'] ); ?></div>
            <div class="lbl">Visitors Today</div>
        </div>
    </div>

    <?php if ( $graph_svg ) : ?>
    <div class="graph-wrap">
        <h2 style="margin-top:1rem">Daily Views (30 days)</h2>
        <?php echo $graph_svg; ?>
    </div>
    <?php endif; ?>

    <?php if ( ! empty( $wp_stats['top_pages'] ) ) : ?>
    <h2 style="margin-top:1rem">Top Pages</h2>
    <ul class="top-pages">
        <?php foreach ( $wp_stats['top_pages'] as $page ) : ?>
        <li>
            <span class="pg-uri"><?php echo esc_html( $page['uri'] ?: '/' ); ?></span>
            <span class="pg-count"><?php echo number_format( (int) $page['total'] ); ?></span>
        </li>
        <?php endforeach; ?>
    </ul>
    <?php endif; ?>
</div>
<?php endif; ?>

<?php if ( ! empty( $oh ) ) : ?>
<div class="card" id="op-oh-card">
    <h2>OnionHeaven</h2>
    <div class="status-grid">
        <?php if ( ! empty( $oh['server_active'] ) ) : ?>
        <div class="status-stat">
            <div class="label">Mode</div>
            <div class="value" id="op-oh-mode">Hub (server)</div>
        </div>
        <div class="status-stat">
            <div class="label">Registered Sites</div>
            <div class="value" id="op-oh-registered"><?php echo (int) ( $oh['registered_count'] ?? 0 ); ?></div>
        </div>
        <div class="status-stat">
            <div class="label">Online</div>
            <div class="value" id="op-oh-online"><?php echo (int) ( $oh['online_count'] ?? 0 ); ?></div>
        </div>
        <div class="status-stat" id="op-oh-takeover-row" <?php if ( empty( $oh['taken_over_count'] ) ) echo 'style="display:none"'; ?>>
            <div class="label">Taken Over</div>
            <div class="value" id="op-oh-taken-over"><?php echo (int) ( $oh['taken_over_count'] ?? 0 ); ?></div>
        </div>
        <div class="status-stat" id="op-oh-workers-row" <?php if ( empty( $oh['takeover_containers'] ) ) echo 'style="display:none"'; ?>>
            <div class="label">Takeover Workers</div>
            <div class="value" id="op-oh-workers"><?php echo (int) ( $oh['takeover_containers'] ?? 0 ); ?></div>
        </div>
        <?php else : ?>
        <div class="status-stat">
            <div class="label">Mode</div>
            <div class="value" id="op-oh-mode">Client</div>
        </div>
        <div class="status-stat">
            <div class="label">Hub</div>
            <div class="value" id="op-oh-hub" style="font-size:0.7rem;word-break:break-all"><?php echo esc_html( $oh['client_hub'] ?? '' ); ?></div>
        </div>
        <div class="status-stat">
            <div class="label">Registration</div>
            <div class="value" id="op-oh-client-status"><?php
                if ( empty( $oh['client_enabled'] ) ) {
                    echo 'Disabled';
                } elseif ( ! empty( $oh['client_registered'] ) ) {
                    echo 'Registered';
                } else {
                    echo 'Pending';
                }
            ?></div>
        </div>
        <?php endif; ?>
    </div>
</div>
<?php endif; ?>

<div class="card logs-wrap">
    <h2>Recent Logs</h2>
    <button type="button" class="most-recent-btn" id="op-logs-bottom" onclick="var l=document.getElementById('op-logs');l.scrollTop=l.scrollHeight;">Most Recent</button>
    <pre class="log-pre" id="op-logs"><?php echo esc_html( $logs ?: 'Waiting for log data...' ); ?></pre>
</div>

<?php endif; ?>

<p style="text-align:center;margin:24px 0 8px;"><a href="/wp-admin/admin.php?page=onionpress-settings" target="_blank" style="color:#666;text-decoration:none;">Settings &rarr;</a></p>

<script>
(function(){
    var stateColors = {running:'#8b5cf6',starting:'#eab308',stopped:'#6b7280',offline:'#6b7280',stuck:'#ef4444',unknown:'#6b7280'};
    function fmtUptime(s) {
        if (!s || s <= 0) return '\u2014';
        var d=Math.floor(s/86400),h=Math.floor((s%86400)/3600),m=Math.floor((s%3600)/60);
        return d>0?d+'d '+h+'h '+m+'m':h>0?h+'h '+m+'m':m+'m';
    }
    function esc(t){var e=document.createElement('span');e.textContent=t;return e.innerHTML;}
    window.opRefresh = function(btn){
        btn.disabled=true;btn.textContent='Refreshing...';
        // Request full status rewrite, then re-poll after watcher picks it up
        fetch('/onionpress-status-json?refresh=1')
        .then(function(){
            setTimeout(function(){ poll(); btn.disabled=false; btn.textContent='Refresh Now'; },12000);
        })
        .catch(function(){ btn.disabled=false; btn.textContent='Refresh Now'; });
    };
    function poll(){
        fetch('/onionpress-status-json')
        .then(function(r){return r.json()})
        .then(function(d){
            var s=d.status;
            if(!s)return;
            var st=s.state||'unknown',col=stateColors[st]||'#6b7280';
            var el;
            el=document.getElementById('op-state');
            if(el)el.innerHTML='<span class="state-dot" style="background:'+col+'"></span>'+esc(st.charAt(0).toUpperCase()+st.slice(1));
            el=document.getElementById('op-version');
            if(el)el.textContent=s.version||'?';
            el=document.getElementById('op-version2');
            if(el)el.textContent=s.version||'?';
            el=document.getElementById('op-uptime');
            if(el)el.textContent=fmtUptime(s.uptime_seconds);
            el=document.getElementById('op-bootstrap');
            if(el)el.textContent=(s.bootstrap_pct||0)+'%';
            // Tor Implementation
            el=document.getElementById('op-tor-impl');
            if(el){var ti=s.tor_impl||'tor';el.textContent=(ti==='tor'?'C Tor':ti);}
            var wq=s.wayback_queue_count||0;
            el=document.getElementById('op-wayback-row');
            if(el)el.style.display=wq>0?'':'none';
            el=document.getElementById('op-wayback');
            if(el)el.textContent=wq+' item'+(wq!=1?'s':'');
            var addr=s.onion_address||'';
            el=document.getElementById('op-onion-row');
            if(el)el.style.display=addr.indexOf('.onion')!==-1?'':'none';
            el=document.getElementById('op-addr');
            if(el&&addr)el.textContent=addr;
            var cw=document.getElementById('op-containers-wrap');
            var cc=document.getElementById('op-containers');
            if(cc&&s.containers){
                var h='';
                for(var name in s.containers){
                    var cs=s.containers[name];
                    var cls=cs==='running'?'ok':'bad';
                    h+='<span class="container-badge '+cls+'">'+esc(name.replace('onionpress-',''))+': '+esc(cs)+'</span>';
                }
                cc.innerHTML=h;
                if(cw)cw.style.display='';
            }
            // OnionHeaven stats
            var oh=s.onionheaven;
            if(oh){
                var ohCard=document.getElementById('op-oh-card');
                if(ohCard)ohCard.style.display='';
                el=document.getElementById('op-oh-mode');
                if(el)el.textContent=oh.server_active?'Hub (server)':'Client';
                if(oh.server_active){
                    el=document.getElementById('op-oh-registered');if(el)el.textContent=oh.registered_count||0;
                    el=document.getElementById('op-oh-online');if(el)el.textContent=oh.online_count||0;
                    el=document.getElementById('op-oh-takeover-row');if(el)el.style.display=(oh.taken_over_count>0)?'':'none';
                    el=document.getElementById('op-oh-taken-over');if(el)el.textContent=oh.taken_over_count||0;
                    el=document.getElementById('op-oh-workers-row');if(el)el.style.display=(oh.takeover_containers>0)?'':'none';
                    el=document.getElementById('op-oh-workers');if(el)el.textContent=oh.takeover_containers||0;
                } else {
                    el=document.getElementById('op-oh-hub');if(el)el.textContent=oh.client_hub||'';
                    el=document.getElementById('op-oh-client-status');
                    if(el)el.textContent=!oh.client_enabled?'Disabled':oh.client_registered?'Registered':'Pending';
                }
            }
            // Load average and host uptime
            if(s.load_avg&&s.load_avg.length){
                el=document.getElementById('op-load');
                if(el)el.textContent=s.load_avg.slice(0,3).map(function(v){return v.toFixed(2)}).join(' / ');
            }
            if(s.host_uptime_seconds>0){
                el=document.getElementById('op-host-uptime');
                if(el){
                    var hu=s.host_uptime_seconds,hd=Math.floor(hu/86400),hh=Math.floor((hu%86400)/3600),hm=Math.floor((hu%3600)/60);
                    el.textContent=hd>0?hd+'d '+hh+'h '+hm+'m':hh>0?hh+'h '+hm+'m':hm+'m';
                }
            }
            el=document.getElementById('op-live-dot');
            if(el)el.textContent='Auto Updated '+new Date().toLocaleTimeString();
            if(d.logs){
                el=document.getElementById('op-logs');
                if(el){
                    var atBottom=el.scrollHeight-el.scrollTop-el.clientHeight<50;
                    el.textContent=d.logs;
                    if(atBottom)el.scrollTop=el.scrollHeight;
                }
            }
        }).catch(function(){});
    }
    setInterval(poll,10000);

    // Show "Most Recent" button when user scrolls up in logs
    var logEl=document.getElementById('op-logs');
    var btnEl=document.getElementById('op-logs-bottom');
    if(logEl&&btnEl){
        logEl.addEventListener('scroll',function(){
            var atBottom=logEl.scrollHeight-logEl.scrollTop-logEl.clientHeight<50;
            btnEl.style.display=atBottom?'none':'block';
        });
    }
})();
</script>

</body>
</html>
    <?php
}

/**
 * Render an inline SVG line graph of daily page views.
 */
function onionpress_render_traffic_graph( $daily_views ) {
    $width  = 660;
    $height = 120;
    $pad_x  = 0;
    $pad_y  = 10;

    $max_val = 1;
    foreach ( $daily_views as $d ) {
        if ( $d['views'] > $max_val ) {
            $max_val = $d['views'];
        }
    }

    $count  = count( $daily_views );
    $step_x = ( $width - 2 * $pad_x ) / max( $count - 1, 1 );
    $usable = $height - 2 * $pad_y;

    $points = array();
    $fill_points = array();
    $fill_points[] = $pad_x . ',' . ( $height - $pad_y );

    foreach ( $daily_views as $i => $d ) {
        $x = $pad_x + $i * $step_x;
        $y = ( $height - $pad_y ) - ( $d['views'] / $max_val ) * $usable;
        $points[] = round( $x, 1 ) . ',' . round( $y, 1 );
        $fill_points[] = round( $x, 1 ) . ',' . round( $y, 1 );
    }

    $fill_points[] = round( $pad_x + ( $count - 1 ) * $step_x, 1 ) . ',' . ( $height - $pad_y );

    $polyline_str = implode( ' ', $points );
    $fill_str     = implode( ' ', $fill_points );

    $svg  = '<svg viewBox="0 0 ' . $width . ' ' . $height . '" style="width:100%;height:auto;margin-top:0.5rem">';
    $svg .= '<polygon points="' . $fill_str . '" fill="rgba(139,92,246,0.15)" />';
    $svg .= '<polyline points="' . $polyline_str . '" fill="none" stroke="#8b5cf6" stroke-width="2" stroke-linejoin="round" />';
    $svg .= '</svg>';

    return $svg;
}
