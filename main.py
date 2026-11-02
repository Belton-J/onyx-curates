import json
import os
import re
import shutil
import urllib.parse
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Request, Form, File, UploadFile, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session, joinedload
from supabase import create_client, Client

from database import engine, get_db, Base
import models

# -----------------------------------------------------------------------------
# Configuration & Initialization
# -----------------------------------------------------------------------------
load_dotenv()

# Supabase Storage Configuration (Direct cloud upload for ephemeral Render.com disks)
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# Normalize URL if raw project reference was supplied
if SUPABASE_URL and not SUPABASE_URL.startswith("http"):
    SUPABASE_URL = f"https://{SUPABASE_URL.strip()}.supabase.co"

supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL.strip(), SUPABASE_KEY.strip())
    except Exception as e:
        print(f"[Supabase Init Warning] Failed to initialize Supabase client: {e}")

# Amazon Associates & PA-API 5.0 Settings
# Replace these with your actual affiliate credentials from Amazon Associates:
# https://affiliate-program.amazon.com/assoc_credentials/home
AMAZON_ACCESS_KEY = os.getenv("AMAZON_ACCESS_KEY", "YOUR_AMAZON_ACCESS_KEY")
AMAZON_SECRET_KEY = os.getenv("AMAZON_SECRET_KEY", "YOUR_AMAZON_SECRET_KEY")
AMAZON_PARTNER_TAG = os.getenv("AMAZON_PARTNER_TAG", "onyxcurates-20")
AMAZON_HOST = os.getenv("AMAZON_HOST", "webservices.amazon.com")
AMAZON_REGION = os.getenv("AMAZON_REGION", "us-east-1")

UPLOAD_DIR = Path("static/uploads")
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Create database tables automatically upon startup
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="Onyx Curates - Pin-to-Post Affiliate Engine",
    description="High-converting Pinterest affiliate marketing platform with 1:1 look mapping and Amazon compliance.",
    version="1.0.0"
)

# Static & Template Mounting
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# -----------------------------------------------------------------------------
# Startup Event / Default Category Seeding
# -----------------------------------------------------------------------------
@app.on_event("startup")
def seed_default_categories():
    """Seeds starter categories if the database is brand new."""
    db: Session = next(get_db())
    try:
        if db.query(models.Category).count() == 0:
            default_categories = [
                models.Category(name="Tech", slug="tech"),
                models.Category(name="Decor", slug="decor"),
                models.Category(name="Gifts", slug="gifts"),
                models.Category(name="Fashion", slug="fashion")
            ]
            db.add_all(default_categories)
            db.commit()
    finally:
        db.close()


# -----------------------------------------------------------------------------
# Amazon Scraper Integration (Bypasses PA-API until 3 qualifying sales)
# -----------------------------------------------------------------------------
def _sync_fetch_amazon_data(clean_asin: str) -> dict:
    """
    Synchronously navigates to Amazon using Playwright in a worker thread.
    Overrides currency to USD and language to en_US via locale, timezone, and cookies.
    """
    url = f"https://www.amazon.com/dp/{clean_asin}"
    affiliate_url = f"https://www.amazon.com/dp/{clean_asin}?tag={AMAZON_PARTNER_TAG}"

    # Default fallback metadata
    product_title = f"Curated Amazon Item [{clean_asin}]"
    price = None
    amazon_image_url = "https://m.media-amazon.com/images/I/71r5E5r8-9L._AC_SL1500_.jpg"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage"
                ]
            )
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York"
            )

            # Explicitly override Amazon currency and language preferences to USD / en_US
            context.add_cookies([
                {"name": "i18n-prefs", "value": "USD", "domain": ".amazon.com", "path": "/"},
                {"name": "lc-main", "value": "en_US", "domain": ".amazon.com", "path": "/"},
                {"name": "skin", "value": "noskin", "domain": ".amazon.com", "path": "/"}
            ])

            page = context.new_page()

            # Mask navigator.webdriver
            page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

            # Navigate to Amazon product page
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # 1. Scrape Product Title
            title_elem = page.query_selector("#productTitle")
            if title_elem:
                raw_title = title_elem.inner_text()
                if raw_title and raw_title.strip():
                    product_title = raw_title.strip()

            # 2. Scrape Price: Primary strategy uses whole + fraction (most reliable on Amazon desktop)
            whole_el = page.query_selector(".a-price .a-price-whole")
            if whole_el:
                whole_txt = re.sub(r"[^\d]", "", whole_el.inner_text())
                fraction_el = page.query_selector(".a-price .a-price-fraction")
                frac_txt = re.sub(r"[^\d]", "", fraction_el.inner_text()) if fraction_el else "00"
                if whole_txt:
                    try:
                        price = float(f"{whole_txt}.{frac_txt}")
                    except (ValueError, TypeError):
                        pass

            # Fallback to offscreen/buybox price selectors with regex stripping
            if price is None:
                price_selectors = [
                    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
                    "#corePrice_desktop .a-price .a-offscreen",
                    "#apex_desktop .a-price .a-offscreen",
                    ".priceToPay .a-offscreen",
                    "#price_inside_buybox",
                    "#priceblock_ourprice",
                    "#priceblock_dealprice"
                ]
                for selector in price_selectors:
                    el = page.query_selector(selector)
                    if el:
                        txt = el.inner_text()
                        cleaned_txt = re.sub(r"[^\d.]", "", txt.replace(",", ""))
                        match = re.search(r"(\d+(?:\.\d{1,2})?)", cleaned_txt)
                        if match:
                            try:
                                price = float(match.group(1))
                                break
                            except (ValueError, TypeError):
                                pass

            # 3. Scrape High-Res Image URL
            img_elem = page.query_selector("#landingImage, #imgBlkFront")
            if img_elem:
                # Priority A: data-old-hires attribute
                hires = img_elem.get_attribute("data-old-hires")
                if hires and hires.strip():
                    amazon_image_url = hires.strip()
                else:
                    # Priority B: parse data-a-dynamic-image JSON dict (choose highest resolution)
                    dynamic_json = img_elem.get_attribute("data-a-dynamic-image")
                    if dynamic_json:
                        try:
                            data = json.loads(dynamic_json)
                            if data:
                                sorted_imgs = sorted(data.items(), key=lambda item: item[1][0] * item[1][1], reverse=True)
                                amazon_image_url = sorted_imgs[0][0]
                        except Exception:
                            pass

                    # Priority C: fallback to standard src
                    if not amazon_image_url or amazon_image_url.startswith("data:"):
                        src = img_elem.get_attribute("src")
                        if src and not src.startswith("data:"):
                            amazon_image_url = src

            browser.close()
    except Exception as e:
        print(f"[Playwright Scraper] Exception for ASIN {clean_asin}: {str(e)}")

    return {
        "asin": clean_asin,
        "product_title": product_title,
        "price": price,
        "amazon_image_url": amazon_image_url,
        "affiliate_url": affiliate_url
    }


async def fetch_amazon_data(asin: str) -> dict:
    """
    Non-blocking async wrapper that delegates to sync_playwright in a thread pool.
    Guarantees compatibility on Windows across all Uvicorn reload loop configurations.
    """
    clean_asin = asin.strip().upper()
    return await asyncio.to_thread(_sync_fetch_amazon_data, clean_asin)


# -----------------------------------------------------------------------------
# Public Routes (Pinterest Landing & Curated Looks)
# -----------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def get_index(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Homepage: Displays all curated Looks (Posts) across all categories.
    Eagerly loads category and associated products for optimal DB queries.
    """
    posts = (
        db.query(models.Post)
        .options(joinedload(models.Post.category), joinedload(models.Post.products))
        .order_by(models.Post.created_at.desc())
        .all()
    )
    categories = db.query(models.Category).order_by(models.Category.name).all()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "posts": posts,
            "categories": categories,
            "current_category": None
        }
    )


@app.get("/category/{slug}", response_class=HTMLResponse)
def get_category_posts(
    slug: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Category Page: Displays Looks filtered by a specific category slug.
    """
    category = db.query(models.Category).filter(models.Category.slug == slug).first()
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")

    posts = (
        db.query(models.Post)
        .filter(models.Post.category_id == category.id)
        .options(joinedload(models.Post.category), joinedload(models.Post.products))
        .order_by(models.Post.created_at.desc())
        .all()
    )
    categories = db.query(models.Category).order_by(models.Category.name).all()

    return templates.TemplateResponse(
        request=request,
        name="category.html",
        context={
            "category": category,
            "posts": posts,
            "categories": categories
        }
    )


@app.get("/post/{post_id}", response_class=HTMLResponse)
def get_post_detail(
    post_id: int,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Post Detail Page: The primary 1:1 target destination for Pinterest traffic.
    Presents the aesthetic Look alongside the exact shoppable affiliate items.
    """
    post = (
        db.query(models.Post)
        .options(joinedload(models.Post.category), joinedload(models.Post.products))
        .filter(models.Post.id == post_id)
        .first()
    )
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    return templates.TemplateResponse(
        request=request,
        name="post.html",
        context={
            "post": post
        }
    )


# -----------------------------------------------------------------------------
# Admin Routes (Private CMS Dashboard)
# -----------------------------------------------------------------------------
@app.get("/admin", response_class=HTMLResponse)
def get_admin_dashboard(
    request: Request,
    msg: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Admin CMS Dashboard: Lists all existing looks, their affiliate items,
    and provides inline forms to add new looks and link affiliate products.
    """
    posts = (
        db.query(models.Post)
        .options(joinedload(models.Post.category), joinedload(models.Post.products))
        .order_by(models.Post.id.desc())
        .all()
    )
    categories = db.query(models.Category).order_by(models.Category.name).all()

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "posts": posts,
            "categories": categories,
            "msg": msg,
            "error": error
        }
    )


@app.get("/admin/create-post", response_class=HTMLResponse)
def get_create_post_form(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Form view dedicated to creating a new aesthetic Look and uploading a pin image.
    Redirects back to admin dashboard with post creation form highlighted.
    """
    categories = db.query(models.Category).order_by(models.Category.name).all()
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "posts": db.query(models.Post).options(joinedload(models.Post.category), joinedload(models.Post.products)).order_by(models.Post.id.desc()).all(),
            "categories": categories,
            "focus_create": True
        }
    )


@app.post("/admin/create-post")
async def post_create_look(
    title: str = Form(...),
    category_id: int = Form(...),
    pin_image: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """
    Handles Pin Post creation and uploads image directly to Supabase Storage.
    Bypasses local disk storage so image assets persist across ephemeral Render.com restarts.
    """
    title_clean = title.strip()
    if not title_clean:
        return RedirectResponse(url="/admin?error=Title+cannot+be+empty", status_code=status.HTTP_303_SEE_OTHER)

    category = db.query(models.Category).filter(models.Category.id == category_id).first()
    if not category:
        return RedirectResponse(url="/admin?error=Invalid+category+selected", status_code=status.HTTP_303_SEE_OTHER)

    # Validate file extension
    file_ext = Path(pin_image.filename).suffix.lower()
    allowed_extensions = {".jpg", ".jpeg", ".png", ".webp"}
    if file_ext not in allowed_extensions:
        return RedirectResponse(
            url="/admin?error=Unsupported+image+format.+Use+JPG,+PNG,+or+WEBP",
            status_code=status.HTTP_303_SEE_OTHER
        )

    # Content-type mapping for proper browser rendering
    content_type_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp"
    }
    content_type = pin_image.content_type or content_type_map.get(file_ext, "image/jpeg")

    # Generate a unique non-colliding filename
    unique_filename = f"{uuid.uuid4().hex}{file_ext}"

    try:
        # Read uploaded image bytes
        file_bytes = await pin_image.read()
        if not file_bytes:
            return RedirectResponse(
                url="/admin?error=Uploaded+image+file+is+empty",
                status_code=status.HTTP_303_SEE_OTHER
            )

        # Upload directly to Supabase Storage bucket 'pin-images'
        if not supabase:
            raise ValueError("Supabase client is not configured. Please set SUPABASE_URL and SUPABASE_KEY.")

        supabase.storage.from_("pin-images").upload(
            path=unique_filename,
            file=file_bytes,
            file_options={"content-type": content_type}
        )

        # Retrieve the permanent public URL
        pin_image_url = supabase.storage.from_("pin-images").get_public_url(unique_filename)

    except Exception as e:
        error_msg = urllib.parse.quote_plus(f"Failed to upload image to Supabase: {str(e)}")
        return RedirectResponse(
            url=f"/admin?error={error_msg}",
            status_code=status.HTTP_303_SEE_OTHER
        )

    # Save to database with Supabase public URL
    new_post = models.Post(
        title=title_clean,
        category_id=category.id,
        pin_image_url=pin_image_url
    )
    db.add(new_post)
    db.commit()
    db.refresh(new_post)

    return RedirectResponse(
        url=f"/admin?msg=Post+'{new_post.title}'+(ID:+{new_post.id})+created+successfully",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/admin/post/{post_id}/add-product")
async def post_add_product_to_look(
    post_id: int,
    asin: str = Form(...),
    hook_text: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    Attaches an Amazon affiliate product to an existing curated Post.
    Pulls metadata via headless Playwright scraper.
    """
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        return RedirectResponse(
            url=f"/admin?error=Post+with+ID+{post_id}+not+found",
            status_code=status.HTTP_303_SEE_OTHER
        )

    clean_asin = asin.strip().upper()
    clean_hook = hook_text.strip()

    if not clean_asin or len(clean_asin) < 8:
        return RedirectResponse(
            url=f"/admin?error=Please+enter+a+valid+Amazon+ASIN",
            status_code=status.HTTP_303_SEE_OTHER
        )

    if not clean_hook:
        clean_hook = "Featured in this curated aesthetic"

    # Fetch live product metadata using headless Playwright scraper
    amazon_data = await fetch_amazon_data(clean_asin)

    new_product = models.Product(
        post_id=post.id,
        asin=clean_asin,
        product_title=amazon_data["product_title"],
        hook_text=clean_hook,
        price=amazon_data.get("price"),
        amazon_image_url=amazon_data["amazon_image_url"],
        affiliate_url=amazon_data["affiliate_url"]
    )

    db.add(new_product)
    db.commit()

    return RedirectResponse(
        url=f"/admin?msg=Affiliate+product+(ASIN:+{clean_asin})+linked+to+Post+#{post_id}",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/admin/post/{post_id}/delete")
def post_delete_look(
    post_id: int,
    db: Session = Depends(get_db)
):
    """
    Deletes an aesthetic Look, cleans up its uploaded pin image file, and cascades
    to delete all attached affiliate products.
    """
    post = db.query(models.Post).filter(models.Post.id == post_id).first()
    if not post:
        return RedirectResponse(
            url="/admin?error=Post+not+found",
            status_code=status.HTTP_303_SEE_OTHER
        )

    # Clean up uploaded image file from Supabase Storage or local disk
    if post.pin_image_url:
        if supabase and "pin-images/" in post.pin_image_url:
            try:
                storage_path = post.pin_image_url.split("pin-images/")[-1]
                supabase.storage.from_("pin-images").remove([storage_path])
            except Exception:
                pass
        elif post.pin_image_url.startswith("/static/uploads/"):
            filename = post.pin_image_url.replace("/static/uploads/", "")
            file_path = UPLOAD_DIR / filename
            if file_path.exists() and file_path.is_file():
                try:
                    file_path.unlink()
                except Exception:
                    pass

    post_title = post.title
    db.delete(post)
    db.commit()

    return RedirectResponse(
        url=f"/admin?msg=Post+'{post_title}'+(ID:+{post_id})+deleted+successfully",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/admin/product/{product_id}/delete")
def post_delete_product(
    product_id: int,
    db: Session = Depends(get_db)
):
    """
    Removes a single affiliated product from its associated Post.
    """
    product = db.query(models.Product).filter(models.Product.id == product_id).first()
    if not product:
        return RedirectResponse(
            url="/admin?error=Product+not+found",
            status_code=status.HTTP_303_SEE_OTHER
        )

    post_id = product.post_id
    asin = product.asin
    db.delete(product)
    db.commit()

    return RedirectResponse(
        url=f"/admin?msg=Product+{asin}+removed+from+Post+#{post_id}",
        status_code=status.HTTP_303_SEE_OTHER
    )


@app.post("/admin/category/create")
def post_create_category(
    name: str = Form(...),
    slug: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    """
    Utility endpoint to quickly create new categories from the Admin CMS.
    """
    name_clean = name.strip()
    if not name_clean:
        return RedirectResponse(url="/admin?error=Category+name+is+required", status_code=status.HTTP_303_SEE_OTHER)

    if not slug or not slug.strip():
        slug_clean = name_clean.lower().replace(" ", "-").replace("/", "-")
    else:
        slug_clean = slug.strip().lower()

    existing = db.query(models.Category).filter(models.Category.slug == slug_clean).first()
    if existing:
        return RedirectResponse(url=f"/admin?error=Category+slug+'{slug_clean}'+already+exists", status_code=status.HTTP_303_SEE_OTHER)

    new_cat = models.Category(name=name_clean, slug=slug_clean)
    db.add(new_cat)
    db.commit()

    return RedirectResponse(url=f"/admin?msg=Category+'{name_clean}'+created", status_code=status.HTTP_303_SEE_OTHER)
