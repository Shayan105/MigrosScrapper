from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from bs4 import BeautifulSoup
import time
import json
import re 
import math
import concurrent.futures
from pymongo import MongoClient
import os
import json

def clean_price_to_float(raw_string):
    """Cleans Swiss price formats (1.-, 16.–, 1.−) into sortable floats."""
    if not raw_string:
        return None
    cleaned = raw_string.strip()
    # Regex: Look for a dot followed by any non-digit chars at the end and replace with .00
    cleaned = re.sub(r'\.\D+$', '.00', cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return cleaned

def parse_product(article):
    """Parses a single product card into a dictionary."""
    product_dict = {}
    
    # ID
    link_tag = article.find('a', {'data-testid': 'product-link'})
    product_dict['id'] = link_tag.get('href').rstrip('/').split('/')[-1] if link_tag else None
    
    # Brand & Name
    brand = article.find('span', class_='name')
    product_dict['brand'] = brand.text.strip() if brand else None
    name = article.find('span', attrs={'data-testid': lambda x: x and x.startswith('product-name')})
    product_dict['name'] = name.text.strip() if name else None
    
    # Price
    price_tag = article.find('span', {'data-testid': 'current-price'})
    product_dict['price'] = clean_price_to_float(price_tag.text) if price_tag else None
        
    # Quantity (Multipack Math)
    quantity_tag = article.find('span', {'data-testid': 'default-product-size'})
    if quantity_tag:
        raw_qty = quantity_tag.text.strip()
        multipack_match = re.search(r'(\d+)\s*[xX]\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)', raw_qty)
        if multipack_match:
            total = int(multipack_match.group(1)) * float(multipack_match.group(2))
            unit = multipack_match.group(3)
            product_dict['quantity'] = f"{int(total) if total.is_integer() else total}{unit}"
        else:
            product_dict['quantity'] = raw_qty
    else:
        product_dict['quantity'] = None
    
    # Price per Unit & Unit
    product_dict['price_per_unit'], product_dict['unit'] = None, None
    ppu_tag = article.find('span', id=lambda x: x and x.endswith('-price-unit'))
    if ppu_tag:
        raw_ppu = ppu_tag.text.strip() 
        if '/' in raw_ppu:
            ppu_part, unit_part = raw_ppu.split('/', 1)
            product_dict['price_per_unit'] = clean_price_to_float(ppu_part)
            product_dict['unit'] = unit_part.strip() 
        else:
            product_dict['price_per_unit'] = clean_price_to_float(raw_ppu)
    
    # Image URL
    img = article.find('img')
    product_dict['image_url'] = img.get('src') if img else None
    
    return product_dict



def create_driver():
    print("Setting up the browser...")
    chrome_options = Options()
    chrome_options.add_argument("--headless=new") 
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
    
    driver = webdriver.Chrome(options=chrome_options)
    return driver


def get_remaining_products_count(driver,url):
    rendered_html = get_rendered_page_sync(driver, url)
    soup = BeautifulSoup(rendered_html, 'html.parser')
    remaining_div = soup.find('div', class_='remaining-products')
    if remaining_div:
        raw_text = remaining_div.text.strip() 
        clean_numbers = re.sub(r'\D', '', raw_text) 
        if clean_numbers:
            return math.ceil(int(clean_numbers) / 100)

    return 0

# --- 2. The Selenium Scraper ---
def get_rendered_page_sync(driver,url): 
    try:
        print(f"Loading {url}...")
        driver.get(url)
        
        # Give Angular enough time to build the list of products
        print("Waiting for products to load...")
        time.sleep(2) 
        
        html_content = driver.page_source
        return html_content
    except Exception as e:
        print(f"Error loading page: {e}")
        return None


def scrape_single_page(url):
    """This function runs in parallel. It MUST create its own driver."""
    print(f"Thread starting for: {url}")
    
    # Create a unique driver for this specific thread
    driver = create_driver()
    page_products = []
    
    try:
        # Step A: Get the HTML
        rendered_html = get_rendered_page_sync(driver, url)
        
        if rendered_html:
            soup = BeautifulSoup(rendered_html, 'html.parser')
            
            # Step C: Find ALL product cards
            product_cards = soup.find_all('article', class_='product-card')
            
            # Step D: Loop through them and parse
            for card in product_cards:
                parsed_data = parse_product(card)
                if parsed_data and parsed_data.get('name'):
                    page_products.append(parsed_data)
                    
        return page_products # Return the list of dictionaries for this page

    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return []
        
    finally:
        # CRITICAL: Always close the thread's driver so you don't run out of RAM!
        driver.quit()



def fetch_product_data(base_url, path_to_json="migros_products.json", max_workers=1, fetch_online=True):
    """
    Fetches product data either by scraping Migros or loading a local JSON file.
    """
    
    # --- LOCAL FILE OVERRIDE ---
    if not fetch_online:
        print(f"Bypassing online scrape. Attempting to load local file: '{path_to_json}'...")
        
        if os.path.exists(path_to_json):
            with open(path_to_json, 'r', encoding='utf-8') as f:
                all_products = json.load(f)
            print(f"✅ Successfully loaded {len(all_products)} products from local JSON.")
            return all_products 
        else:
            print(f"⚠️ Warning: Local file '{path_to_json}' not found! Forcing online fetch...")
           

    # --- LIVE SCRAPING LOGIC ---
    print(f"Fetching online data from: {base_url}")

    temp_driver = create_driver()  
    # Calculate pages
    number_of_pages = get_remaining_products_count(temp_driver, base_url) + 1
    temp_driver.quit() 

    # Generate all URLs
    urls = [f"{base_url}?page={i}" for i in range(1, int(number_of_pages) + 1)]
    print(f"Total pages to scrape: {len(urls)}")
    all_products = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        results = executor.map(scrape_single_page, urls)
        for page_results in results:
            if page_results:
                all_products.extend(page_results)

    print(f"\nSuccessfully extracted {len(all_products)} total products.")

    # Save the fresh data to JSON
    with open(path_to_json, "w", encoding="utf-8") as f:
        json.dump(all_products, f, indent=4, ensure_ascii=False)
    print(f"All data successfully saved to '{path_to_json}'!")
    

    return all_products









def save_to_mongodb(products_list, db_name="migros_db", collection_name="products"):
    """Saves a list of dictionaries to MongoDB as daily snapshots."""
    
    # Connect to the local MongoDB server
    client = MongoClient("mongodb://localhost:27017/")
    db = client[db_name]
    collection = db[collection_name]
    
    print(f"Connected to MongoDB. Saving {len(products_list)} product snapshots...")
    
    # Capture the exact time this snapshot is being taken
    scrape_timestamp = datetime.utcnow()
    
    for product in products_list:
        if not product.get('id'):
            continue
            
        # 1. Add the timestamp so you can group/sort by date later
        product['scraped_at'] = scrape_timestamp
        
        # 2. We DO NOT set product['_id']. 
        # By leaving '_id' out, MongoDB will auto-generate a unique ObjectId for this snapshot.
        
        # 3. Use insert_one instead of update_one/upsert to append a new history record
        collection.insert_one(product)
        
    print("Snapshot data successfully saved to MongoDB!")



    
MAX_WORKERS = 5
URL_PATES_CONDIMENTS_CONSERVES = "https://www.migros.ch/fr/category/pates-condiments-conserves"
URL_PRODUITS_LAITIERS_OEUFS_PLATS = "https://www.migros.ch/fr/category/produits-laitiers-ufs-plats-prep"
prod = fetch_product_data(base_url=URL_PRODUITS_LAITIERS_OEUFS_PLATS,path_to_json="produits_laitiers_ufs_plats.json", max_workers=MAX_WORKERS, fetch_online=True)
save_to_mongodb(prod, db_name="migros_db", collection_name="produits_laitiers_oeufs_plats")