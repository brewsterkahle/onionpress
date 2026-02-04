# Building the Onion.Press Product Website

This guide explains how to create the retro-styled product website in WordPress.

## Prerequisites

1. Onion.Press installed and running
2. Access to WordPress admin at http://localhost:8080/wp-admin
3. Internet connection (for downloading theme and fonts)

## Step 1: WordPress Setup

### Login to WordPress
1. Go to http://localhost:8080/wp-admin
2. Login with your WordPress credentials

### Install Theme
The site uses **Twenty Twenty-Four** (comes with WordPress by default)

1. Go to Appearance → Themes
2. Activate "Twenty Twenty-Four"

## Step 2: Install Self-Hosted Fonts

**Important**: Install fonts BEFORE adding CSS!

Follow the complete guide in `FONT-SETUP.md` to:
1. Download Press Start 2P and VT323 fonts
2. Convert to web formats (WOFF2/WOFF)
3. Upload to WordPress at `/wp-content/uploads/fonts/`

This ensures privacy - no external font requests!

## Step 3: Add Custom CSS

1. Go to Appearance → Customize
2. Click "Additional CSS"
3. Copy the entire contents of `custom-retro-style.css`
4. Paste into the Additional CSS box
5. Click "Publish"

You should immediately see the retro styling take effect (with pixel fonts!)

## Step 4: Create Pages

Create these pages in WordPress (Pages → Add New):

### Home Page

**Title**: Home (or leave blank for front page)

**Content** (use the Block Editor):

```html
<!-- Hero Section -->
<div class="hero">
  <h1>★ ✨ ONION.PRESS ✨ ★</h1>
  <p style="font-size: 24px; font-weight: bold;">Your Own Corner of the Internet</p>
  <p style="font-size: 18px;">Private • Fun • Under Your Control</p>
  <br>
  <a href="/download" class="wp-element-button">💾 DOWNLOAD NOW</a>
  <a href="/how-it-works" class="wp-element-button" style="background: #00FFFF !important;">📖 LEARN MORE</a>
  <br><br>
  <p>🌐 Works on Mac • Easy Setup • v2.2.33</p>
</div>

<!-- Features Grid -->
<div class="feature-grid" style="margin: 40px 0;">
  <div class="feature-box">
    <h3>🏠 Your Own .onion Site</h3>
    <p>Get your unique dark web address. Like GeoCities, but anonymous.</p>
  </div>

  <div class="feature-box">
    <h3>🔒 Private by Default</h3>
    <p>No tracking, no ads, no surveillance. Tor keeps you anonymous.</p>
  </div>

  <div class="feature-box">
    <h3>💾 All Yours</h3>
    <p>Run WordPress on your Mac. You control everything.</p>
  </div>

  <div class="feature-box">
    <h3>🆓 Forever Free</h3>
    <p>No hosting fees. No monthly charges. Open source software.</p>
  </div>

  <div class="feature-box">
    <h3>⚡ Easy Setup</h3>
    <p>Download, install, launch. Your site is live in 5 minutes.</p>
  </div>

  <div class="feature-box">
    <h3>🌐 Full WordPress</h3>
    <p>Familiar interface. Any theme. Any plugin. Total freedom.</p>
  </div>
</div>

<!-- Visitor Counter (cosmetic) -->
<div style="text-align: center; margin: 40px 0;">
  <div class="visitor-counter">👁️ 000042</div>
  <p><small>You are visitor #42 (this counter is just for fun!)</small></p>
</div>

<!-- Badges -->
<div style="text-align: center; margin: 40px 0;">
  <span class="retro-badge">POWERED BY TOR</span>
  <span class="retro-badge">BEST VIEWED WITH TOR BROWSER</span>
  <span class="retro-badge">OPEN SOURCE</span>
</div>
```

**Settings**:
- Set as Front Page: Settings → Reading → "A static page" → select Home

---

### Features Page

See `content-proposal.md` for full content. Key sections:

- Easy Setup
- Full WordPress
- True Privacy
- Optional Vanity Addresses
- Retro Features (Coming Soon!)

---

### How It Works Page

**Content**: Step-by-step guide with emojis

1. Download Onion.Press
2. Install & Launch
3. First Launch Magic
4. Your Site is Live!
5. Share Your Address

---

### Download Page

**Large Download Button**:

```html
<div style="text-align: center; margin: 40px 0;">
  <a href="https://github.com/brewsterkahle/onion.press/releases/latest" class="wp-element-button" style="font-size: 20px !important; padding: 30px 60px !important;">
    💾 DOWNLOAD FOR MAC<br>
    <small style="font-family: Courier;">onion.press.dmg (82 MB)</small><br>
    <small style="font-family: Courier;">macOS 13+ | Intel & M-series</small>
  </a>
</div>
```

**Installation Instructions**:
- Numbered steps
- System requirements
- Troubleshooting links

---

### About Page

**Content**:
- Mission statement
- Why Tor?
- Why WordPress?
- The Story
- Open Source philosophy
- Credits

---

### Contact Page

**GitHub Issues** (primary contact method):

```html
<div style="border: 3px solid black; padding: 30px; background: #FFFFE0; margin: 20px 0;">
  <h3>🐛 Found a Bug?</h3>
  <h3>💡 Have a Feature Request?</h3>
  <h3>❓ Need Help?</h3>
  <br>
  <p style="font-size: 18px;">Open an issue on GitHub:</p>
  <a href="https://github.com/brewsterkahle/onion.press/issues" class="wp-element-button">OPEN AN ISSUE</a>
</div>
```

---

### Donate Page

**Content**:
- Mission statement
- How donations help
- Ways to donate (Internet Archive, GitHub Sponsors)
- Other ways to help (star, share, contribute)

## Step 5: Create Menu

1. Go to Appearance → Menus
2. Create a new menu called "Main Menu"
3. Add pages in this order:
   - Home
   - Features
   - How It Works
   - Download
   - About
   - Contact
   - Donate
4. Set as Primary Menu
5. Save Menu

## Step 6: Customize Site Identity

1. Go to Appearance → Customize → Site Identity
2. Site Title: "ONION.PRESS"
3. Tagline: "Your Own Corner of the Internet"
4. Upload site icon (if you have one)

## Step 7: Optional Graphics

### Create/Find These Graphics:

1. **Hero Background** - Gradient or starfield
2. **Feature Icons** - Pixel art style icons
3. **Badges**:
   - "NEW!" animated GIF
   - "Powered by Tor"
   - "Best Viewed with Tor Browser"
   - Under construction (ironic)

### Where to Find Retro Graphics:

- **gifcities.org** - Archive of GeoCities GIFs
- **Pixel Art Makers** - piskelapp.com, pixilart.com
- **Free Icons** - noun project, flaticon (pixelate them)
- **Make your own** - Use GIMP or Photoshop with pixelation filter

### Upload to WordPress:

1. Media → Add New
2. Upload graphics
3. Insert into pages using Block Editor

## Step 8: Test Mobile Responsiveness

1. Use browser dev tools (F12)
2. Toggle device toolbar
3. Test on phone and tablet sizes
4. Adjust CSS if needed

## Step 9: Export to Git

Once you're happy with the site:

```bash
cd product-website/scripts
./export-site.sh
```

Then commit and push:

```bash
git add product-website/
git commit -m "Initial product website design"
git push
```

## Tips & Tricks

### Adding Animated Elements

For blinking text (use sparingly!):

```html
<span class="blink">NEW!</span>
```

### Color Customization

Change colors in the CSS variables at the top of `custom-retro-style.css`:

```css
:root {
    --retro-magenta: #FF00FF;    /* Change to your color */
    --retro-cyan: #00FFFF;       /* Change to your color */
    /* etc. */
}
```

### Font Options

The CSS uses self-hosted retro pixel fonts:
- **Press Start 2P** - Authentic 80s arcade/game font (headings/buttons)
- **VT323** - Classic terminal font (code blocks/counter)

Both fonts are served from your WordPress installation (no external requests).
Fallbacks to Impact and Courier New if fonts aren't loaded yet.

See `FONT-SETUP.md` for installation instructions.

### Accessibility

Despite the retro style, ensure:
- [ ] Sufficient color contrast
- [ ] Proper heading hierarchy (H1 → H2 → H3)
- [ ] Alt text on all images
- [ ] Keyboard navigation works
- [ ] Links are clearly identifiable

### Performance on Tor

Remember: Tor is slower than regular internet.

- Optimize images (compress before upload)
- Use WebP format when possible
- Minimize large GIFs
- Keep total page size under 1 MB

## Troubleshooting

### CSS not applying?

1. Clear WordPress cache
2. Hard refresh browser (Cmd+Shift+R)
3. Check for CSS syntax errors

### Mobile layout broken?

Check the responsive CSS at the bottom of `custom-retro-style.css`.

### Need help?

Open an issue on GitHub!

## Next Steps

After building the site:

1. Test all pages and links
2. Have someone review for typos
3. Test on actual Tor Browser
4. Share your .onion address!
5. Export and commit to git
6. Deploy to production instance

---

**Have fun building! Remember: embrace the weird!** ✨
