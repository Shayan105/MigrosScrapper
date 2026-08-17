from fastapi import FastAPI, HTTPException, Query
from pymongo import MongoClient
from typing import List, Optional
from datetime import datetime, time
from enum import Enum

app = FastAPI(
    title="Migros Scraper API", 
    version="1.2.0",
    description="API to query scraped products, price histories, deals, and analytics from Migros."
)

DB_NAME = "migros_db"
MONGO_URL = "mongodb://127.0.0.1:27017/"

def get_db():
    return MongoClient(MONGO_URL, serverSelectionTimeoutMS=5000)[DB_NAME]

def get_valid_collections(db) -> List[str]:
    return [c for c in db.list_collection_names() if not c.startswith("system.")]

def get_dynamic_category_enum():
    try:
        db = get_db()
        collections = get_valid_collections(db)
        if not collections:
            collections = ["fruits_legumes", "boulangerie_patisserie", "viandes_poissons"]
        return Enum("Category", {c: c for c in collections})
    except Exception:
        return Enum("Category", {
            "fruits_legumes": "fruits_legumes",
            "boulangerie_patisserie": "boulangerie_patisserie",
            "viandes_poissons": "viandes_poissons"
        })

CategoryEnum = get_dynamic_category_enum()

def format_mongo_doc(doc):
    if doc:
        doc["_id"] = str(doc["_id"])
        if isinstance(doc.get("scraped_at"), datetime):
            doc["scraped_at"] = doc["scraped_at"].isoformat()
    return doc

def build_date_query(date_str: str = None, start_str: str = None, end_str: str = None):
    query = {}
    if date_str:
        day_start = datetime.combine(datetime.strptime(date_str, "%Y-%m-%d"), time.min)
        day_end = datetime.combine(day_start, time.max)
        query["scraped_at"] = {"$gte": day_start, "$lte": day_end}
    elif start_str or end_str:
        range_query = {}
        if start_str:
            range_query["$gte"] = datetime.combine(datetime.strptime(start_str, "%Y-%m-%d"), time.min)
        if end_str:
            range_query["$lte"] = datetime.combine(datetime.strptime(end_str, "%Y-%m-%d"), time.max)
        query["scraped_at"] = range_query
    return query


# ==========================================
# 1. RETROCOMPATIBILITÉ (Routes inchangées)
# ==========================================

@app.get("/categories", tags=["Metadata"], summary="List all available product categories")
def list_categories():
    try:
        db = get_db()
        return {"categories": get_valid_collections(db)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.get("/products/{category}", tags=["Products"], summary="Get products by category with optional filters")
def get_products(
    category: CategoryEnum,
    page: int = Query(1, ge=1, description="Page number for pagination"),
    limit: int = Query(20, ge=1, le=100, description="Number of items per page"),
    date: Optional[str] = Query(None, description="Filter items scraped on a specific day. Format: YYYY-MM-DD"),
    start_date: Optional[str] = Query(None, description="Start date for range queries. Format: YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="End date for range queries. Format: YYYY-MM-DD"),
    is_reduced: Optional[bool] = Query(None, description="Filter items that have action prices or discounts")
):
    try:
        db = get_db()
        category_str = category.value
        
        valid_categories = get_valid_collections(db)
        if category_str not in valid_categories:
            raise HTTPException(status_code=404, detail=f"Category '{category_str}' not found")

        match_filter = {}
        if is_reduced is not None:
            match_filter["is_reduced"] = is_reduced

        date_query = build_date_query(date, start_date, end_date)
        has_date_filter = bool(date_query)
        
        if has_date_filter:
            match_filter.update(date_query)
            skip = (page - 1) * limit
            cursor = db[category_str].find(match_filter).sort("scraped_at", -1).skip(skip).limit(limit)
            
            products = [format_mongo_doc(p) for p in cursor]
            total = db[category_str].count_documents(match_filter)
        else:
            pipeline = [
                {"$match": match_filter},
                {"$sort": {"scraped_at": -1}},
                {
                    "$group": {
                        "_id": "$id",
                        "latest_doc": {"$first": "$$ROOT"}
                    }
                },
                {"$replaceRoot": {"newRoot": "$latest_doc"}},
                {"$sort": {"scraped_at": -1}},
                {"$skip": (page - 1) * limit},
                {"$limit": limit}
            ]
            
            total_pipeline = [
                {"$match": match_filter},
                {"$group": {"_id": "$id"}},
                {"$count": "count"}
            ]
            
            cursor = db[category_str].aggregate(pipeline)
            products = [format_mongo_doc(p) for p in cursor]
            
            count_result = list(db[category_str].aggregate(total_pipeline))
            total = count_result[0]["count"] if count_result else 0

        return {
            "category": category_str,
            "mode": "snapshot" if has_date_filter else "latest_distinct",
            "total_matches": total,
            "results": products
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


# ==========================================
# 2. NOUVELLES ROUTES (Améliorations)
# ==========================================

@app.get("/products/item/{product_id}/history", tags=["Analytics"], summary="Get full price history & analytics for a product ID")
def get_product_price_history(product_id: str):
    """Recherche l'historique chronologique des prix d'un produit dans toutes les collections."""
    try:
        db = get_db()
        collections = get_valid_collections(db)
        
        raw_history = []
        found_category = None
        product_name = None

        for cat in collections:
            docs = list(db[cat].find({"id": str(product_id)}).sort("scraped_at", 1))
            if not docs:
                # Essai avec ID sous forme de nombre au cas où
                try:
                    docs = list(db[cat].find({"id": int(product_id)}).sort("scraped_at", 1))
                except ValueError:
                    pass

            if docs:
                found_category = cat
                product_name = docs[-1].get("name", "Inconnu")
                for doc in docs:
                    formatted = format_mongo_doc(doc)
                    raw_history.append({
                        "scraped_at": formatted.get("scraped_at"),
                        "price": formatted.get("price"),
                        "original_price": formatted.get("original_price"),
                        "is_reduced": formatted.get("is_reduced", False)
                    })
                break

        if not raw_history:
            raise HTTPException(status_code=404, detail=f"Product with ID '{product_id}' not found")

        prices = [h["price"] for h in raw_history if isinstance(h.get("price"), (int, float))]
        min_price = min(prices) if prices else None
        max_price = max(prices) if prices else None
        current_price = raw_history[-1]["price"] if raw_history else None

        return {
            "product_id": str(product_id),
            "product_name": product_name,
            "category": found_category,
            "analytics": {
                "current_price": current_price,
                "lowest_price_seen": min_price,
                "highest_price_seen": max_price,
                "total_scrapes": len(raw_history)
            },
            "history": raw_history
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/products/item/{product_id}", tags=["Products"], summary="Get latest details of a single product by ID")
def get_product_by_id(product_id: str):
    """Obtiend la fiche la plus récente d'un produit à partir de son ID."""
    try:
        db = get_db()
        collections = get_valid_collections(db)

        for cat in collections:
            doc = db[cat].find_one({"id": str(product_id)}, sort=[("scraped_at", -1)])
            if not doc:
                try:
                    doc = db[cat].find_one({"id": int(product_id)}, sort=[("scraped_at", -1)])
                except ValueError:
                    pass

            if doc:
                formatted = format_mongo_doc(doc)
                formatted["category"] = cat
                return formatted

        raise HTTPException(status_code=404, detail=f"Product with ID '{product_id}' not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/search", tags=["Products"], summary="Search products across all categories")
def search_products(
    q: str = Query(..., min_length=2, description="Search term (product name or ID)"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100)
):
    """Recherche un produit par mot-clé dans son nom à travers toutes les catégories."""
    try:
        db = get_db()
        collections = get_valid_collections(db)
        results = []

        regex_query = {"$regex": q, "$options": "i"}
        match_filter = {"$or": [{"name": regex_query}, {"id": str(q)}]}

        for cat in collections:
            pipeline = [
                {"$match": match_filter},
                {"$sort": {"scraped_at": -1}},
                {"$group": {"_id": "$id", "latest_doc": {"$first": "$$ROOT"}}},
                {"$replaceRoot": {"newRoot": "$latest_doc"}}
            ]
            cursor = db[cat].aggregate(pipeline)
            for doc in cursor:
                formatted = format_mongo_doc(doc)
                formatted["category"] = cat
                results.append(formatted)

        # Pagination mémoire après agrégation multi-collections
        total_matches = len(results)
        skip = (page - 1) * limit
        paginated_results = results[skip:skip + limit]

        return {
            "query": q,
            "total_matches": total_matches,
            "page": page,
            "limit": limit,
            "results": paginated_results
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


@app.get("/deals", tags=["Analytics"], summary="Get all current promotions across all categories")
def get_current_deals(
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100)
):
    """Récupère tous les produits actuellement en promotion (`is_reduced=True`)."""
    try:
        db = get_db()
        collections = get_valid_collections(db)
        deals = []

        for cat in collections:
            pipeline = [
                {"$match": {"is_reduced": True}},
                {"$sort": {"scraped_at": -1}},
                {"$group": {"_id": "$id", "latest_doc": {"$first": "$$ROOT"}}},
                {"$replaceRoot": {"newRoot": "$latest_doc"}}
            ]
            cursor = db[cat].aggregate(pipeline)
            for doc in cursor:
                formatted = format_mongo_doc(doc)
                formatted["category"] = cat
                deals.append(formatted)

        total_deals = len(deals)
        skip = (page - 1) * limit
        paginated_deals = deals[skip:skip + limit]

        return {
            "total_deals": total_deals,
            "page": page,
            "limit": limit,
            "results": paginated_deals
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")

@app.get("/")
def read_root():
    return {"status": "ok", "message": "Migros Scraper API is running"}

@app.get("/stats", tags=["Metadata"], summary="Get overall database stats")
def get_db_stats():
    """Donne des statistiques globales sur le scraper et la base de données."""
    try:
        db = get_db()
        collections = get_valid_collections(db)
        
        total_scraped_records = 0
        categories_stats = {}

        for cat in collections:
            count = db[cat].count_documents({})
            total_scraped_records += count
            categories_stats[cat] = count

        return {
            "total_categories": len(collections),
            "total_scraped_records": total_scraped_records,
            "records_per_category": categories_stats
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="::", port=8000)