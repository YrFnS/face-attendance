import numpy as np

from face_attendance import match_employee, norm


def main():
    known = [
        {"employee": "HR-EMP-1", "embedding": norm(np.array([1.0, 0.0, 0.0]))},
        {"employee": "HR-EMP-2", "embedding": norm(np.array([0.8, 0.6, 0.0]))},
    ]

    score, employee, margin = match_employee(known, np.array([1.0, 0.0, 0.0]))

    assert employee == "HR-EMP-1"
    assert round(score, 3) == 1.0
    assert round(margin, 3) == 0.2


if __name__ == "__main__":
    main()
