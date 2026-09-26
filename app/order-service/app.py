from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
@app.route("/order")
def home():
    return jsonify({
        "service": "order-service",
        "status": "running"
    })

@app.route("/orders")
@app.route("/order/orders")
def orders():
    return jsonify([
        {
            "order_id": 1001,
            "item": "Laptop",
            "quantity": 1,
            "status": "PENDING"
        },
        {
            "order_id": 1002,
            "item": "Mouse",
            "quantity": 2,
            "status": "SHIPPED.."
        }
    ])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
