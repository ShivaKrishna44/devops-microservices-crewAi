from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
@app.route("/payment")
def home():
    return jsonify({
        "service": "payment-service",
        "status": "running"
    })

@app.route("/payments")
@app.route("/payment/payments")
def payments():
    return jsonify([
        {
            "payment_id": 2001,
            "amount": 500,
            "status": "SUCCESS.."
        }
    ])

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)