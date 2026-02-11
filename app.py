from flask import Flask, render_template, request, redirect, url_for, flash, session
from pymongo import MongoClient
import redis
from bson.objectid import ObjectId
from datetime import datetime
from flask_paginate import Pagination, get_page_parameter
from functools import wraps

app = Flask(__name__)
app.secret_key = "change_this_secret_key"

# MongoDB
mongo_client = MongoClient("mongodb://localhost:27017/")
db = mongo_client["Canteen"]
items_col = db["Items"]
users_col = db["Users"]
orders_col = db["Orders"]

# Redis
r = redis.Redis(host="localhost", port=6379, decode_responses=True)


def calculate_total_price(price, quantity):
    return float(price) * int(quantity)


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if "user" not in session:
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def ensure_items_fields():
    # Fix old documents that don't have available_quantity
    for i in items_col.find({"available_quantity": {"$exists": False}}, {"stock": 1}):
        stock = int(i.get("stock", 0) or 0)
        items_col.update_one({"_id": i["_id"]}, {"$set": {"available_quantity": stock}})


# Create default admin if not exists
if not users_col.find_one({"username": "Meet"}):
    users_col.insert_one({"username": "Meet", "password": "Meet@123", "role": "admin"})

ensure_items_fields()


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()

        user = users_col.find_one({"username": username, "password": password})
        if user:
            session["user"] = {"id": str(user["_id"]), "username": user["username"], "role": user.get("role", "user")}
            flash("Logged in!", "success")
            return redirect(url_for("index"))
        flash("Invalid login.", "error")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    session.pop("user", None)
    flash("Logged out.", "info")
    return redirect(url_for("login"))


@app.route("/", methods=["GET", "POST"])
@login_required
def index():
    user = session["user"]
    page = request.args.get(get_page_parameter(), type=int, default=1)
    per_page = 8

    # Add Item (admin only)
    if request.method == "POST" and request.form.get("action") == "add":
        if user["role"] != "admin":
            flash("Access denied.", "error")
            return redirect(url_for("index"))

        stock = int(request.form.get("stock", 1))
        item = {
            "name": request.form["name"].strip(),
            "price": float(request.form["price"]),
            "category": request.form["category"].strip(),
            "stock": stock,
            "available_quantity": stock,
            "created_at": datetime.now(),
        }
        items_col.insert_one(item)
        r.set("latest_item", item["name"])
        flash("Item added!", "success")
        return redirect(url_for("index"))

    # Order
    if request.method == "POST" and request.form.get("action") == "order":
        item_id = request.form["item_id"].strip()
        quantity = int(request.form["quantity"])
        item = items_col.find_one({"_id": ObjectId(item_id)})

        available = int((item or {}).get("available_quantity", 0) or 0)
        if item and available >= quantity:
            total_price = calculate_total_price(item.get("price", 0), quantity)
            order = {
                "user_id": user["id"],
                "item_id": str(item["_id"]),
                "quantity": quantity,
                "total_price": total_price,
                "order_date": datetime.now(),
                "status": "Pending",
            }
            orders_col.insert_one(order)
            items_col.update_one({"_id": item["_id"]}, {"$inc": {"available_quantity": -quantity}})
            flash(f"Ordered! Total: ${total_price:.2f}", "success")
        else:
            flash("Item unavailable or insufficient stock.", "error")
        return redirect(url_for("index"))

    # Search
    q = {}
    sn = request.args.get("search_name", "").strip()
    sc = request.args.get("search_category", "").strip()
    if sn:
        q["name"] = {"$regex": sn, "$options": "i"}
    if sc:
        q["category"] = {"$regex": sc, "$options": "i"}

    total_items = items_col.count_documents(q)

    items = list(
        items_col.find(q)
        .sort("name", 1)
        .skip((page - 1) * per_page)
        .limit(per_page)
    )

    available_total = 0
    for i in items_col.find(q, {"available_quantity": 1}):
        available_total += int(i.get("available_quantity", 0) or 0)

    pagination = Pagination(page=page, per_page=per_page, total=total_items, css_framework="bootstrap4")
    latest_item = r.get("latest_item")

    return render_template(
        "index.html",
        user=user,
        items=items,
        total_items=total_items,
        available_total=available_total,
        pagination=pagination,
        latest_item=latest_item,
        page=page,
        per_page=per_page
    )


@app.route("/items")
@login_required
def items():
    user = session["user"]
    page = request.args.get(get_page_parameter(), type=int, default=1)
    per_page = 15

    q = {}
    sn = request.args.get("search_name", "").strip()
    sc = request.args.get("search_category", "").strip()
    if sn:
        q["name"] = {"$regex": sn, "$options": "i"}
    if sc:
        q["category"] = {"$regex": sc, "$options": "i"}

    total_items = items_col.count_documents(q)
    items = list(
        items_col.find(q)
        .sort("name", 1)
        .skip((page - 1) * per_page)
        .limit(per_page)
    )
    pagination = Pagination(page=page, per_page=per_page, total=total_items, css_framework="bootstrap4")
    return render_template("items.html", user=user, items=items, total_items=total_items, pagination=pagination)


@app.route("/update/<item_id>", methods=["POST"])
@login_required
def update_item(item_id):
    if session["user"]["role"] != "admin":
        flash("Access denied.", "error")
        return redirect(url_for("index"))

    updates = {}
    for k, form_key in [("name", "new_name"), ("price", "new_price"), ("category", "new_category")]:
        v = request.form.get(form_key, "").strip()
        if v:
            updates[k] = float(v) if k == "price" else v

    if updates:
        items_col.update_one({"_id": ObjectId(item_id)}, {"$set": updates})
        flash("Updated!", "success")
    return redirect(url_for("index"))


@app.route("/delete/<item_id>")
@login_required
def delete_item(item_id):
    if session["user"]["role"] != "admin":
        flash("Access denied.", "error")
        return redirect(url_for("index"))

    items_col.delete_one({"_id": ObjectId(item_id)})
    flash("Deleted!", "success")
    return redirect(url_for("index"))


@app.route("/complete_order/<order_id>")
@login_required
def complete_order(order_id):
    if session["user"]["role"] != "admin":
        flash("Access denied.", "error")
        return redirect(url_for("index"))

    order = orders_col.find_one({"_id": ObjectId(order_id)})
    if order and order.get("status") != "Completed":
        orders_col.update_one(
            {"_id": order["_id"]},
            {"$set": {"status": "Completed", "completed_at": datetime.now()}}
        )
        flash("Order completed!", "success")
    return redirect(url_for("my_orders"))


@app.route("/cancel_order/<order_id>")
@login_required
def cancel_order(order_id):
    user = session["user"]

    # Admin can cancel any pending order; user can cancel only their own pending order
    q = {"_id": ObjectId(order_id), "status": "Pending"}
    if user.get("role") != "admin":
        q["user_id"] = user["id"]

    order = orders_col.find_one(q)
    if not order:
        flash("Cannot cancel this order.", "error")
        return redirect(url_for("my_orders"))

    # Restore item stock
    qty = int(order.get("quantity", 0) or 0)
    item_id = order.get("item_id")
    if item_id and qty > 0:
        items_col.update_one({"_id": ObjectId(item_id)}, {"$inc": {"available_quantity": qty}})

    orders_col.update_one(
        {"_id": order["_id"]},
        {"$set": {"status": "Cancelled", "cancelled_at": datetime.now()}}
    )
    flash("Order cancelled.", "info")
    return redirect(url_for("my_orders"))


@app.route("/my_orders")
@login_required
def my_orders():
    user = session["user"]

    # Admin sees all orders; user sees only their orders
    order_filter = {} if user.get("role") == "admin" else {"user_id": user["id"]}
    orders = list(orders_col.find(order_filter).sort("order_date", -1))

    # Attach item name
    item_ids = list({ObjectId(o["item_id"]) for o in orders if o.get("item_id")})
    item_map = {str(i["_id"]): i.get("name", "Unknown") for i in items_col.find({"_id": {"$in": item_ids}}, {"name": 1})}
    for o in orders:
        o["item_name"] = item_map.get(o.get("item_id", ""), "Unknown")

    return render_template("my_orders.html", user=user, orders=orders)


if __name__ == "__main__":
    app.run(debug=True)
