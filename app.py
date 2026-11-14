import base64, hashlib, json, os, secrets, sqlite3
from datetime import datetime, timezone
from functools import wraps
from flask import Flask, jsonify, redirect, render_template, request, session, url_for, flash
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-this")
DB_PATH = os.getenv("SQLITE_PATH", os.path.join(os.path.dirname(__file__), "secure_ecommerce.db"))
def get_db():
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection
def init_db():
    with get_db() as connection:
        connection.executescript("CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, salt TEXT NOT NULL, created_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, items TEXT NOT NULL, total REAL NOT NULL, created_at TEXT NOT NULL, security TEXT NOT NULL);")
init_db()

DEMO_PRODUCTS = [
 {"_id":"lamp","name":"AeroForm Desk Lamp","category":"Workspace","price":129,"description":"A sculptural, warm-dim LED lamp for focused evenings.","icon":"◒","accent":"coral","image":"https://images.unsplash.com/photo-1507473885765-e6ed057f782c?auto=format&fit=crop&w=900&q=85"},
 {"_id":"phones","name":"Mossline Headphones","category":"Audio","price":219,"description":"Quiet, spacious sound with all-day comfort.","icon":"◉","accent":"mint","image":"https://images.unsplash.com/photo-1505740420928-5e560c06d30e?auto=format&fit=crop&w=900&q=85"},
 {"_id":"bag","name":"Grid 02 Carryall","category":"Travel","price":164,"description":"Structured recycled canvas, built for the daily commute.","icon":"▦","accent":"sun","image":"https://images.unsplash.com/photo-1553062407-98eeb64c6a62?auto=format&fit=crop&w=900&q=85"},
 {"_id":"cup","name":"Orbit Tumbler","category":"Objects","price":48,"description":"Double-wall steel with a satisfying magnetic lid.","icon":"○","accent":"sky","image":"https://images.unsplash.com/photo-1514228742587-6b1558fcca3d?auto=format&fit=crop&w=900&q=85"},
]

def b64(value): return base64.b64encode(value).decode()
def derive_password(password, salt):
    return Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode())
def get_products():
    return DEMO_PRODUCTS
def find_product(pid): return next((p for p in get_products() if str(p.get("_id")) == pid), None)
def current_user():
    if not session.get("user_id"): return None
    with get_db() as connection: record = connection.execute("SELECT id AS _id, name, email, password_hash, salt, created_at FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    return dict(record) if record else None
def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"): return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped
@app.context_processor
def globals(): return {"user": current_user(), "cart_count": sum(session.get("cart", {}).values())}
@app.route("/")
def home():
    query = request.args.get("q", "").strip().lower()
    products = [p for p in get_products() if not query or query in p["name"].lower() or query in p["category"].lower()]
    return render_template("index.html", products=products, query=query)
@app.route("/product/<pid>")
def product(pid):
    item = find_product(pid)
    return render_template("product.html", product=item) if item else ("Not found", 404)
@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        email, password, name = request.form["email"].lower().strip(), request.form["password"], request.form["name"].strip()
        salt = secrets.token_bytes(16)
        try:
            with get_db() as connection:
                connection.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)", (secrets.token_hex(12), name, email, b64(derive_password(password, salt)), b64(salt), datetime.now(timezone.utc).isoformat()))
            flash("Account created. Please sign in.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError: flash("That email is already registered.", "error")
    return render_template("register.html")
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].lower().strip()
        with get_db() as connection: record = connection.execute("SELECT id AS _id, password_hash, salt FROM users WHERE email = ?", (email,)).fetchone()
        try: valid = record and secrets.compare_digest(b64(derive_password(request.form["password"],base64.b64decode(record["salt"]))), record["password_hash"])
        except Exception: valid = False
        if valid: session["user_id"] = str(record["_id"]); return redirect(request.args.get("next") or url_for("home"))
        flash("Invalid credentials.", "error")
    return render_template("login.html")
@app.route("/logout")
def logout(): session.clear(); return redirect(url_for("home"))
@app.route("/cart/add/<pid>", methods=["POST"])
def add_cart(pid):
    if not find_product(pid): return jsonify(error="Product not found"), 404
    cart = session.setdefault("cart", {}); cart[pid] = cart.get(pid, 0) + 1; session.modified = True
    return redirect(request.referrer or url_for("home"))
@app.route("/cart")
def cart():
    items=[]
    for pid, qty in session.get("cart", {}).items():
        p=find_product(pid)
        if p: items.append({"product":p,"quantity":qty,"total":p["price"]*qty})
    return render_template("cart.html", items=items, subtotal=sum(i["total"] for i in items))
@app.route("/checkout", methods=["GET","POST"])
@login_required
def checkout():
    items=[]
    for pid, qty in session.get("cart", {}).items():
        p=find_product(pid)
        if p: items.append({"product":p,"quantity":qty,"total":p["price"]*qty})
    if request.method == "POST" and items:
        order_data=json.dumps({"items":[{"name":i["product"]["name"],"qty":i["quantity"],"price":i["product"]["price"]} for i in items],"total":sum(i["total"] for i in items)}, separators=(",",":"))
        process=hybrid_encrypt(order_data)
        order={"_id": secrets.token_hex(6), "user_id":session["user_id"],"items":items,"total":sum(i["total"] for i in items),"created_at":datetime.now(timezone.utc),"security":process}
        with get_db() as connection:
            connection.execute("INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)", (order["_id"], order["user_id"], json.dumps(order["items"]), order["total"], order["created_at"].isoformat(), json.dumps(order["security"])))
        session.pop("cart", None); session["last_security"] = process
        return redirect(url_for("confirmation"))
    return render_template("checkout.html", items=items, subtotal=sum(i["total"] for i in items))
@app.route("/confirmation")
@login_required
def confirmation(): return render_template("confirmation.html", process=session.get("last_security", {}))
@app.route("/security-process")
@login_required
def security_process(): return render_template("security_process.html", process=session.get("last_security", {}))
@app.route("/orders")
@login_required
def orders():
    with get_db() as connection:
        rows = connection.execute("SELECT id AS _id, items, total, created_at FROM orders WHERE user_id = ? ORDER BY created_at DESC", (session["user_id"],)).fetchall()
    saved_orders = [{"_id": row["_id"], "items": json.loads(row["items"]), "total": row["total"], "created_at": datetime.fromisoformat(row["created_at"])} for row in rows]
    return render_template("orders.html", orders=saved_orders)
@app.route("/profile")
@login_required
def profile(): return render_template("profile.html")

def rsa_pair():
    private = rsa.generate_private_key(public_exponent=65537,key_size=2048); return private, private.public_key()
def hybrid_encrypt(text):
    aes_key=AESGCM.generate_key(bit_length=256); iv=secrets.token_bytes(12); ciphertext=AESGCM(aes_key).encrypt(iv,text.encode(),None)
    private, public=rsa_pair(); wrapped=public.encrypt(aes_key,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None))
    digest=hashlib.sha256(text.encode()).hexdigest(); signature=private.sign(text.encode(),padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.MAX_LENGTH),hashes.SHA256())
    public_pem=public.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode(); private_pem=private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode()
    return {"plaintext":text,"hash":digest,"ciphertext":b64(ciphertext),"aes_key":b64(aes_key),"iv":b64(iv),"encrypted_key":b64(wrapped),"signature":b64(signature),"public_key":public_pem,"private_key":private_pem,"verified":True,"status":"Secure transaction sealed"}
def lab_rsa():
    private,public=rsa_pair(); sample=AESGCM.generate_key(bit_length=256); wrapped=public.encrypt(sample,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None)); recovered=private.decrypt(wrapped,padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),algorithm=hashes.SHA256(),label=None)); return {"public_key":public.public_bytes(serialization.Encoding.PEM,serialization.PublicFormat.SubjectPublicKeyInfo).decode(),"private_key":private.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()).decode(),"encrypted_key":b64(wrapped),"decrypted_key":b64(recovered),"match":secrets.compare_digest(sample,recovered)}
@app.route("/security-lab")
@login_required
def security_lab(): return render_template("security_lab.html")
@app.post("/api/security/<action>")
@login_required
def security_api(action):
    data=request.get_json() or {}; text=str(data.get("text", "Security Lab"))
    if action == "sha256":
        text_bytes = text.encode("utf-8")
        changed = text + ("." if text else "x")
        changed_bytes = changed.encode("utf-8")
        return jsonify(hash=hashlib.sha256(text_bytes).hexdigest(), changed_hash=hashlib.sha256(changed_bytes).hexdigest(), changed_input=changed)
    if action == "aes":
        key=AESGCM.generate_key(bit_length=256); iv=secrets.token_bytes(12); cipher=AESGCM(key).encrypt(iv,text.encode(),None); plain=AESGCM(key).decrypt(iv,cipher,None); return jsonify(key=b64(key),iv=b64(iv),ciphertext=b64(cipher),decrypted=plain.decode())
    if action == "rsa": return jsonify(lab_rsa())
    if action == "hybrid": return jsonify(hybrid_encrypt(text))
    if action == "signature":
        private,public=rsa_pair(); sig=private.sign(text.encode(),padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.MAX_LENGTH),hashes.SHA256()); modified=data.get("modified",text+" (edited)")
        def verify(value):
            try: public.verify(sig,value.encode(),padding.PSS(mgf=padding.MGF1(hashes.SHA256()),salt_length=padding.PSS.MAX_LENGTH),hashes.SHA256()); return True
            except Exception: return False
        return jsonify(hash=hashlib.sha256(text.encode()).hexdigest(),signature=b64(sig),valid=verify(text),modified=modified,modified_valid=verify(modified))
    return jsonify(error="Unknown operation"), 400
if __name__ == "__main__": app.run(debug=False)
